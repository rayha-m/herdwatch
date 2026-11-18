"""Per-sample behaviour rules -> one label per cow-minute -> bouts.

Per 2-s sample (YOLO box + posture prob + zones + motion):
  walking   : standing and foot point moved > 0.35 body widths since previous sample (2 s)
  feeding   : standing with box overlapping the feed-bunk zone (>= 0.30)       [Cam 02 primary]
  drinking  : standing with box overlapping the waterer zone (>= 0.30)
  social    : standing, not at bunk, box IoU >= 0.25 with another standing cow, sustained
  lying / standing otherwise.
Minute label (priority, provenance kept):
  agent answer (if a hard case covered this minute)            -> source = agent
  feeding / drinking / walking if >= 1/3 of samples            -> source = observed
  social if >= 1/2 of samples                                  -> source = estimated (proximity rule)
  lying / standing majority; if the chewing detector or agent
  confirmed cud-chewing in that idle bout -> ruminating_*      -> source = observed (chew) / estimated
  no samples                                                   -> other_not_visible
grooming is only ever assigned by the agent (not observable at 0.5 fps boxes).
"""
import numpy as np, pandas as pd

WALK_BW = 0.50
FEED_OV, WATER_OV, SOC_IOU = 0.30, 0.60, 0.40
WALK_MIN_FRAC = 0.25        # walking comes in short bursts: >= ~15 s of the minute
DRINK_MAX_RUN = 3           # minutes; longer stays at the waterer are loitering (standing)
# Cam 02 looks down on the trough: the box covers the standing body, only the head is in the trough
FEED_OV_CAM = {"Cam 02": 0.15}


def sample_labels(d):
    d = d.sort_values(["track", "ts"]).copy()
    d["standing"] = d.p_stand >= 0.5
    d["standing_sure"] = d.p_stand >= 0.8
    prev = d.groupby("track")[["fx", "fy", "ts"]].shift(1)
    dt = (d.ts - prev.ts).dt.total_seconds()
    move = np.hypot(d.fx - prev.fx, d.fy - prev.fy) / d.w.clip(lower=20)
    d["moving"] = (move > WALK_BW) & (dt <= 4.5)
    # social proximity: pairwise IoU among standing cows in the same frame
    d["near_other"] = False
    for (cam, ts), g in d[d.standing & (d.bunk_ov < FEED_OV)].groupby(["cam", "ts"]):
        if len(g) < 2:
            continue
        b = g[["x1", "y1", "x2", "y2"]].values.astype(float)
        x1 = np.maximum(b[:, None, 0], b[None, :, 0]); y1 = np.maximum(b[:, None, 1], b[None, :, 1])
        x2 = np.minimum(b[:, None, 2], b[None, :, 2]); y2 = np.minimum(b[:, None, 3], b[None, :, 3])
        inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        a = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
        iou = inter / (a[:, None] + a[None] - inter); np.fill_diagonal(iou, 0)
        d.loc[g.index, "near_other"] = iou.max(1) >= SOC_IOU
    fthr = d.cam.map(FEED_OV_CAM).fillna(FEED_OV).values
    at_bunk = d.bunk_ov.values >= fthr
    lab = np.where(~d.standing, "lying", "standing").astype(object)
    # NOTE: proximity is NOT social interaction (blind audit: 0/8 correct). Social contact and
    # grooming are assigned only by the agent, which looks at the animals over time.
    lab = np.where(d.standing_sure & (d.water_ov >= WATER_OV), "drinking", lab)
    lab = np.where(d.standing & at_bunk, "feeding", lab)
    lab = np.where(d.standing_sure & d.moving & ~at_bunk, "walking", lab)
    d["s_label"] = lab
    return d


def minute_labels(d, agent_minutes=None, chew_minutes=None, all_minutes=None, cows=None):
    """d: sample rows with cow, ts, cam, s_label, p_stand, ir.  Returns cow-minute table."""
    d = d[d.cow > 0].copy()
    d["minute"] = d.ts.dt.floor("min")
    rows = []
    POST_PRI = ["Cam 03", "Cam 01", "Cam 02"]
    for (cow, m), g in d.groupby(["cow", "minute"]):
        n = len(g); vc = g.s_label.value_counts()
        frac = (vc / n).to_dict()
        cams = ",".join(sorted(g.cam.unique()))
        night = bool(g.ir.mean() > 0.5)
        per_cam = {c: gc.s_label.value_counts(normalize=True).to_dict() for c, gc in g.groupby("cam")}
        feed_votes = [per_cam[c].get("feeding", 0) for c in per_cam]
        c2_feed = per_cam.get("Cam 02", {}).get("feeding")
        # without the top-down bunk camera, bunk-zone presence is weaker evidence: demand most of the minute
        if (c2_feed is not None and c2_feed >= 1 / 3) or (c2_feed is None and max(feed_votes) >= 0.6):
            lab, src = "feeding", "observed"
        elif frac.get("drinking", 0) >= 0.5:
            lab, src = "drinking", "estimated"      # at the waterer; muzzle-in-water not resolvable at 0.5 fps
        elif frac.get("walking", 0) >= WALK_MIN_FRAC:
            lab, src = "walking", "observed"
        elif frac.get("social_interaction", 0) >= 0.6:
            lab, src = "social_interaction", "estimated"
        else:
            pc = next((c for c in POST_PRI if (g.cam == c).sum() >= 3), None)
            gp = g[g.cam == pc] if pc else g
            lying = (gp.p_stand < 0.5).mean() >= 0.5
            lab, src = ("lying" if lying else "standing"), "observed"
        conf = float(max(frac.values()))
        rows.append(dict(cow=cow, minute=m, behavior=lab, source=src, confidence=round(conf, 2),
                         n_samples=n, cameras=cams, night=night,
                         p_lying=round(float((g.p_stand < 0.5).mean()), 2),
                         **{f"f_{k}": round(v, 2) for k, v in frac.items()}))
    ml = pd.DataFrame(rows)
    # long stays at the waterer: only the first DRINK_MAX_RUN minutes count as drinking
    if len(ml):
        ml = ml.sort_values(["cow", "minute"]).reset_index(drop=True)
        dr = ml.behavior == "drinking"
        run_id = ((~dr) | (ml.cow != ml.cow.shift()) | (ml.minute.diff() != pd.Timedelta(minutes=1))).cumsum()
        pos = dr.groupby(run_id).cumsum()
        loiter = dr & (pos > DRINK_MAX_RUN)
        ml.loc[loiter, "behavior"] = "standing"; ml.loc[loiter, "source"] = "estimated"
    # rumination: chew detector / agent evidence on idle minutes
    if chew_minutes is not None and len(chew_minutes):
        key = set(zip(chew_minutes.cow, chew_minutes.minute))
        for i in ml.index:
            if (ml.at[i, "cow"], ml.at[i, "minute"]) in key and ml.at[i, "behavior"] in ("lying", "standing"):
                ml.at[i, "behavior"] = "ruminating_" + ml.at[i, "behavior"]; ml.at[i, "source"] = "observed(chew)"
    if agent_minutes is not None and len(agent_minutes):
        am = agent_minutes.set_index(["cow", "minute"])
        for i in ml.index:
            k = (ml.at[i, "cow"], ml.at[i, "minute"])
            if k in am.index:
                a = am.loc[k]
                if isinstance(a, pd.DataFrame):
                    a = a.iloc[0]
                ml.at[i, "behavior"] = a.behavior; ml.at[i, "source"] = "agent"; ml.at[i, "confidence"] = a.confidence
    # fill every minute for every cow with other_not_visible
    if all_minutes is not None:
        cows = cows or sorted(ml.cow.unique())
        full = pd.MultiIndex.from_product([cows, all_minutes], names=["cow", "minute"]).to_frame(index=False)
        ml = full.merge(ml, on=["cow", "minute"], how="left")
        ml["behavior"] = ml.behavior.fillna("other_not_visible")
        ml["source"] = ml.source.fillna("not_observed")
        ml["n_samples"] = ml.n_samples.fillna(0).astype(int)
        ml["confidence"] = ml.confidence.fillna(0.0)
    return ml.sort_values(["cow", "minute"]).reset_index(drop=True)


def bouts(ml):
    ml = ml.sort_values(["cow", "minute"])
    out = []
    for cow, g in ml.groupby("cow"):
        g = g.reset_index(drop=True)
        brk = (g.behavior != g.behavior.shift()) | (g.minute.diff() != pd.Timedelta(minutes=1))
        g["bout"] = brk.cumsum()
        for _, b in g.groupby("bout"):
            out.append(dict(cow_id=f"Cow{int(cow):02d}", behavior=b.behavior.iloc[0],
                            start=b.minute.iloc[0], end=b.minute.iloc[-1] + pd.Timedelta(minutes=1),
                            duration_min=len(b), source=b.source.mode().iloc[0],
                            mean_confidence=round(float(b.confidence.mean()), 2),
                            cameras=",".join(sorted(set(",".join(b.cameras.dropna()).split(",")) - {""})),
                            night=bool(b.night.fillna(False).mean() > 0.5) if "night" in b else False))
    return pd.DataFrame(out)
