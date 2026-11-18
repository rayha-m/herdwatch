"""Level 2: BRD risk from the Level 1 records.

Behavioural indicators of BRD (pre-clinical) used here, all relative to the herd
(the ten cows share pen, feed and weather, so the herd is the control):
  - reduced feeding time and fewer bunk visits          (Quimby et al. 2001; Sowell et al. 1999;
                                                          Wolfger et al. 2015)
  - weaker response to fresh feed (bunk attendance in the 30 min after a delivery)
  - increased lying time / longer lying bouts            (sickness behaviour; Toaff-Rosenstein et al. 2016)
  - social isolation (distance to nearest herd-mate)     (behavioural sickness response, Hart 1988)
  - decline from Day 1 to Day 2 in feeding time          (change within the cow)
  - visible clinical signs found by the agent (Level 3 check), when available

score_raw = sum_k w_k * z_k, z oriented so that "more BRD-like" is positive and
clipped to +/-3; risk = logistic(score_raw / 1.5) so 0.5 = herd-typical.
Each cow carries a data-quality flag (observed hours, mean ID confidence): a score
built on few observed hours is shown as low-reliability, never hidden.

    python -m pipeline.level2 --run full
"""
import argparse, json
import numpy as np, pandas as pd
from .config import OUT

WEIGHTS = {  # literature-guided, not fitted (no ground-truth BRD labels exist for these cows)
    "feeding_min_per_day": 1.0,
    "bunk_visits_per_day": 0.8,
    "feedcall_attendance": 0.8,
    "lying_pct": 0.7,
    "longest_lying_bout_min": 0.4,
    "isolation_nn_bw": 0.6,
    "feeding_change_d2_vs_d1": 0.7,
    "drinking_visits_per_day": 0.3,
}
DIRECTION = {  # +1: higher value = more BRD-like; -1: lower value = more BRD-like
    "feeding_min_per_day": -1, "bunk_visits_per_day": -1, "feedcall_attendance": -1,
    "lying_pct": +1, "longest_lying_bout_min": +1, "isolation_nn_bw": +1,
    "feeding_change_d2_vs_d1": -1, "drinking_visits_per_day": -1,
}
LABEL = {
    "feeding_min_per_day": "time at the feed bunk",
    "bunk_visits_per_day": "number of bunk visits",
    "feedcall_attendance": "response to fresh feed (bunk attendance in the 30 min after delivery)",
    "lying_pct": "share of time lying",
    "longest_lying_bout_min": "longest uninterrupted lying bout",
    "isolation_nn_bw": "distance from nearest herd-mate (away from the bunk)",
    "feeding_change_d2_vs_d1": "change in bunk time from Day 1 to Day 2",
    "drinking_visits_per_day": "waterer visits",
}
UNIT = {"feeding_min_per_day": "min/obs-day", "bunk_visits_per_day": "visits/obs-day", "feedcall_attendance": "% of minutes",
        "lying_pct": "% of observed time", "longest_lying_bout_min": "min", "isolation_nn_bw": "body widths",
        "feeding_change_d2_vs_d1": "% change", "drinking_visits_per_day": "visits/obs-day"}


def feed_events(ml, min_gap_h=3):
    """Feed deliveries inferred from herd behaviour: minute where the share of visible cows
    feeding jumps to >= 0.6 after >= 20 min below 0.3 (fresh feed draws the herd to the bunk)."""
    vis = ml[ml.behavior != "other_not_visible"]
    herd = vis.groupby("minute").behavior.apply(lambda s: (s == "feeding").mean()).sort_index()
    herd = herd.rolling(3, min_periods=1).mean()
    ev, last = [], None
    low_run = 0
    for m, v in herd.items():
        low_run = low_run + 1 if v < 0.3 else 0 if v >= 0.6 else low_run
        if v >= 0.6 and (last is None or (m - last) > pd.Timedelta(hours=min_gap_h)):
            prev = herd[(herd.index < m) & (herd.index >= m - pd.Timedelta(minutes=20))]
            if len(prev) >= 10 and (prev < 0.3).mean() > 0.7:
                ev.append(m); last = m
    return ev, herd


def features(ml, samples=None):
    ml = ml.copy(); ml["minute"] = pd.to_datetime(ml.minute); ml["day"] = ml.minute.dt.date
    obs = ml[ml.behavior != "other_not_visible"]
    rows = []
    events, herd = feed_events(ml)
    for (cow, day), g in ml.groupby(["cow_id", "day"]):
        o = g[g.behavior != "other_not_visible"].sort_values("minute")
        obs_h = len(o) / 60
        if obs_h < 0.5:
            rows.append(dict(cow_id=cow, day=day, obs_hours=obs_h)); continue
        scale = 24 / obs_h          # per observed day
        feed = (o.behavior == "feeding")
        # bouts: consecutive feeding minutes, allowing 2-min gaps
        fm = o.minute[feed]
        visits = int(((fm.diff() > pd.Timedelta(minutes=3)) | fm.diff().isna()).sum()) if len(fm) else 0
        lying = o.behavior.isin(["lying", "ruminating_lying"])
        lm = o.minute[lying]
        lb = (lm.diff() != pd.Timedelta(minutes=1)).cumsum() if len(lm) else pd.Series(dtype=int)
        longest = int(lm.groupby(lb).size().max()) if len(lm) else 0
        dm = o.minute[o.behavior == "drinking"]
        dvis = int(((dm.diff() > pd.Timedelta(minutes=3)) | dm.diff().isna()).sum()) if len(dm) else 0
        fc = []
        for e in events:
            if e.date() != day:
                continue
            w = o[(o.minute >= e) & (o.minute < e + pd.Timedelta(minutes=30))]
            if len(w) >= 5:
                fc.append((w.behavior == "feeding").mean() * 100)
        rows.append(dict(cow_id=cow, day=day, obs_hours=round(obs_h, 1),
                         feeding_min_per_day=feed.sum() * scale, bunk_visits_per_day=visits * scale,
                         feedcall_attendance=np.mean(fc) if fc else np.nan,
                         lying_pct=lying.mean() * 100, longest_lying_bout_min=longest,
                         drinking_visits_per_day=dvis * scale,
                         walking_pct=(o.behavior == "walking").mean() * 100,
                         mean_conf=o.confidence.mean()))
    F = pd.DataFrame(rows)
    # isolation from Cam 03 positions (nearest neighbour distance in body widths, away from the bunk)
    if samples is not None and len(samples):
        s = samples[(samples.cam == "Cam 03") & (samples.cow > 0) & (samples.s_label != "feeding")].copy()
        # boxes cut by the image border have inflated normalised distances: exclude them
        s = s[(s.x1 > 5) & (s.x2 < 1275) & (s.y2 < 955) & ((s.x2 - s.x1) >= 25)]
        s["fx"] = (s.x1 + s.x2) / 2; s["fy"] = s.y2; s["bw"] = (s.x2 - s.x1).clip(lower=20)
        nn = []
        for ts, g in s.groupby("ts"):
            if len(g) < 3:
                continue
            P = g[["fx", "fy"]].values; D = np.linalg.norm(P[:, None] - P[None], axis=-1); np.fill_diagonal(D, np.inf)
            nn.append(pd.DataFrame({"cow": g.cow.values, "day": ts.date(), "nn": D.min(1) / g.bw.values}))
        if nn:
            nn = pd.concat(nn); iso = nn.groupby(["cow", "day"]).nn.median().reset_index()
            iso["cow_id"] = iso.cow.map(lambda k: f"Cow{int(k):02d}")
            F = F.merge(iso[["cow_id", "day", "nn"]].rename(columns={"nn": "isolation_nn_bw"}), on=["cow_id", "day"], how="left")
    if "isolation_nn_bw" not in F:
        F["isolation_nn_bw"] = np.nan
    return F, events, herd


def score(F):
    days = sorted(F.day.dropna().unique())
    agg = F.groupby("cow_id").agg({k: "mean" for k in WEIGHTS if k in F and k != "feeding_change_d2_vs_d1"} |
                                  {"obs_hours": "sum", "mean_conf": "mean"})
    if len(days) >= 2:
        d1 = F[F.day == days[0]].set_index("cow_id").feeding_min_per_day
        d2 = F[F.day == days[-1]].set_index("cow_id").feeding_min_per_day
        agg["feeding_change_d2_vs_d1"] = ((d2 - d1) / d1.clip(lower=1) * 100).reindex(agg.index)
    else:
        agg["feeding_change_d2_vs_d1"] = np.nan
    Z = pd.DataFrame(index=agg.index)
    for k in WEIGHTS:
        x = agg[k]
        med, mad = x.median(), (x - x.median()).abs().median() * 1.4826
        sd = mad if mad and mad > 1e-9 else (x.std() or 1.0)
        Z[k] = (DIRECTION[k] * (x - med) / sd).clip(-3, 3).fillna(0)
    contrib = Z.mul(pd.Series(WEIGHTS))
    raw = contrib.sum(1) / sum(WEIGHTS.values()) * 2.5
    risk = 1 / (1 + np.exp(-raw / 1.0))
    out = pd.DataFrame({"risk_score": risk.round(3), "raw_score": raw.round(2)})
    out["rank"] = out.risk_score.rank(ascending=False, method="first").astype(int)
    reasons = []
    for cow in out.index:
        top = contrib.loc[cow].sort_values(ascending=False)
        txt = []
        for k, c in top.items():
            if c <= 0.15 or len(txt) == 3:
                break
            v, herd = agg.at[cow, k], agg[k].median()
            word = "lower" if DIRECTION[k] < 0 else "higher"
            fmt = (lambda x: f"{x:.1f}") if abs(herd) < 10 else (lambda x: f"{x:.0f}")
            txt.append(f"{LABEL[k]} {word} than herd ({fmt(v)} vs median {fmt(herd)} {UNIT[k]})")
        reasons.append("; ".join(txt) if txt else "no behaviour notably worse than the herd")
    out["top_reasons"] = reasons
    out["observed_hours"] = agg.obs_hours.round(1)
    out["mean_label_confidence"] = agg.mean_conf.round(2)
    out["reliability"] = np.where(agg.obs_hours >= 24, "good", np.where(agg.obs_hours >= 8, "moderate", "low"))
    out = out.join(agg.drop(columns=["obs_hours", "mean_conf"]).round(2))
    return out.sort_values("rank"), Z, contrib


def run(name):
    d = OUT / "level1" / name
    ml = pd.read_csv(d / "activity_minutes.csv", parse_dates=["minute"])
    S = pd.read_parquet(d / "samples.parquet") if (d / "samples.parquet").exists() else None
    F, events, herd = features(ml, S)
    R, Z, C = score(F)
    o = OUT / "level2"; o.mkdir(parents=True, exist_ok=True)
    R.reset_index().rename(columns={"index": "cow_id"}).to_csv(o / "brd_risk.csv", index=False)
    F.to_csv(o / "features_per_cow_day.csv", index=False)
    C.round(3).to_csv(o / "risk_contributions.csv")
    herd.rename("herd_feeding_share").to_csv(o / "herd_feeding_share_by_minute.csv")
    json.dump({"feed_events": [str(e) for e in events], "weights": WEIGHTS}, open(o / "risk_model.json", "w"), indent=1)
    print(R[["risk_score", "rank", "reliability", "observed_hours", "top_reasons"]].to_string())
    return R


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--run", default="full"); a = ap.parse_args()
    run(a.run)
