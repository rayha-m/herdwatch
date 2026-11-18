"""Identity re-anchoring with agent roll calls (fixes slow ID drift of the tracker).

1. Roll calls: every 30 min in daylight, on the Cam 03 frame with the most cows, the agent
   describes each cow's ear tags (colour, number when legible), white markings and other
   marks (pipeline.rollcall). One low-effort call per roll call; cached in work/rollcall.jsonl.
2. Register: observations are clustered into <= 10 identities. Tag numbers are the strongest
   key, then tag-colour set + white markings. Cannot-link: two cows seen in the same roll call
   are never the same identity.
3. Relabel: at each roll call, tracker slots are Hungarian-matched to register identities
   (cost 0 for an observed match). Between roll calls a slot keeps its identity; when it
   changes, the switch is placed at the midpoint. Unanchored slots keep their tracker id.
   Applied to all cameras (Cam 01/02 ids come from Cam 03 slots).
Outputs: outputs/level1/<run>/herd_register.csv and rollcall_observations.csv.
"""
import json, re
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pandas as pd
from scipy.optimize import linear_sum_assignment
from .config import WORK, N_COWS
from .rollcall import roll_call
from .llm import BudgetExceeded

CACHE = WORK / "rollcall.jsonl"
TAGS = ["yellow", "green", "blue", "red", "white"]
TAGMAP = {"pink": "red", "orange": "red"}


def schedule(S, step_min=30, day_hours=(7, 17.5), min_cows=4):
    s = S[(S.cam == "Cam 03") & (~S.ir.astype(bool)) & (S.cow > 0) & ((S.y2 - S.y1) >= 35)]
    out = []
    for day in sorted(s.ts.dt.normalize().unique()):
        t = pd.Timestamp(day) + pd.Timedelta(hours=day_hours[0])
        while t <= pd.Timestamp(day) + pd.Timedelta(hours=day_hours[1]):
            g = s[(s.ts >= t) & (s.ts < t + pd.Timedelta(minutes=3))]
            if len(g):
                cnt = g.groupby("ts").size()
                if cnt.max() >= min_cows:
                    out.append(cnt.idxmax())
            t += pd.Timedelta(minutes=step_min)
    return out


def run_rollcalls(S, times, workers=4):
    done = {}
    if CACHE.exists():
        for l in CACHE.open():
            r = json.loads(l); done.setdefault(r["rc_ts"], []).append(r)
    todo = [t for t in times if str(t) not in done]
    s3 = S[S.cam == "Cam 03"]

    def one(t):
        g = s3[(s3.ts == t) & (s3.cow > 0)]
        try:
            obs = roll_call(g)
        except BudgetExceeded:
            return t, None
        except Exception as e:
            print("roll call failed", t, str(e)[:120]); return t, []
        # attach track ids (roll_call returns tracker_cow; recover track by cow at that ts)
        tk = dict(zip(g.cow.astype(int), g.track))
        for o in obs:
            o["rc_ts"] = str(t); o["track"] = tk.get(o["tracker_cow"])
        return t, obs

    with ThreadPoolExecutor(workers) as ex:
        for t, obs in ex.map(one, todo):
            if obs is None:
                break
            done[str(t)] = obs
            with CACHE.open("a") as f:
                for o in obs:
                    f.write(json.dumps(o) + "\n")
    return pd.DataFrame([o for t in times for o in done.get(str(t), [])])


def _sig(o):
    tags = {TAGMAP.get(c, c) for c in o["tag_colours"] if c != "none_visible"}
    wm = o["white_markings"]
    wm = "face" if wm in ("white_face", "white_blaze_or_patch_on_face") else ("body" if wm in ("white_on_body", "white_on_neck_or_chest") else ("none" if wm == "none" else None))
    nums = set(re.findall(r"(?<!\d)(\d{2,3})(?!\d)", o.get("other_marks", "") or ""))
    return tags, wm, nums


def _dist(a, b):
    ta, wa, na = a; tb, wb, nb = b
    if na and nb:
        return 0.0 if na & nb else 1.0
    d = 0.0
    if ta and tb:
        d += 1 - len(ta & tb) / len(ta | tb)
    else:
        d += 0.5
    if wa is not None and wb is not None:
        d += 0.0 if wa == wb else 1.0
    else:
        d += 0.3
    return d / 2


def build_register(obs, max_ids=N_COWS, thr=0.35):
    o = obs[obs.visible_quality.isin(["good", "partial"])].copy()
    o["sig"] = [_sig(r) for r in o.to_dict("records")]
    o = o[o.sig.map(lambda s: bool(s[0] or s[2] or s[1] in ("face", "body")))]   # needs at least one identifying cue
    o["strength"] = o.sig.map(lambda s: 3 if s[2] else (2 if s[0] and s[1] is not None else 1)) + (o.visible_quality == "good")
    o = o.sort_values("strength", ascending=False)
    clusters = []   # list of dict(members=[idx], sigs=[sig], rcs=set())
    lab = {}
    for i, r in o.iterrows():
        best, bd = None, 9
        for ci, c in enumerate(clusters):
            if r.rc_ts in c["rcs"]:
                continue
            d = np.mean([_dist(r.sig, s) for s in c["sigs"][:12]])
            if d < bd:
                best, bd = ci, d
        if best is not None and bd < thr:
            clusters[best]["members"].append(i); clusters[best]["sigs"].append(r.sig); clusters[best]["rcs"].add(r.rc_ts); lab[i] = best
        else:
            clusters.append(dict(members=[i], sigs=[r.sig], rcs={r.rc_ts})); lab[i] = len(clusters) - 1
    sizes = sorted(range(len(clusters)), key=lambda c: -len(clusters[c]["members"]))
    keep = [c for c in sizes[:max_ids] if len(clusters[c]["members"]) >= 3]
    rid = {c: k + 1 for k, c in enumerate(keep)}
    o["rid"] = [rid.get(lab[i], 0) for i in o.index]
    reg = []
    for c, k in rid.items():
        sigs = clusters[c]["sigs"]
        tags = pd.Series([t for s in sigs for t in s[0]]).value_counts()
        wms = pd.Series([s[1] for s in sigs if s[1]]).value_counts()
        nums = pd.Series([n for s in sigs for n in s[2]]).value_counts()
        reg.append(dict(rid=k, observations=len(sigs), tag_colours=", ".join(tags.index[:2]) if len(tags) else "none seen",
                        white_markings=wms.index[0] if len(wms) else "unknown",
                        tag_numbers=", ".join(nums.index[:2]) if len(nums) else ""))
    return o, pd.DataFrame(reg)


def relabel(S, o, n_slots=N_COWS):
    """Map tracker slot ids -> register ids over time. Returns new cow column for S."""
    o = o[o.rid > 0]
    times = sorted(o.rc_ts.unique())
    seg = {k: [] for k in range(1, n_slots + 1)}       # slot -> list of (time, rid)
    for t in times:
        g = o[o.rc_ts == t]
        slots = sorted(set(g.tracker_cow)); rids = sorted(set(g.rid))
        if not slots:
            continue
        C = np.ones((len(slots), len(rids)))
        for r in g.itertuples():
            C[slots.index(r.tracker_cow), rids.index(r.rid)] -= 1 if r.visible_quality == "good" else 0.6
        ri, ci = linear_sum_assignment(C)
        for i, j in zip(ri, ci):
            if C[i, j] < 0.7:
                seg[slots[i]].append((pd.Timestamp(t), rids[j]))
    # build per-slot piecewise mapping with midpoint switches
    new = S.cow.copy().astype(int)
    used_rids = set(o.rid)
    spare = [k for k in range(1, n_slots + 1) if k not in used_rids]
    for k, obs in seg.items():
        m = S.cow == k
        if not obs:
            continue
        obs.sort()
        # collapse consecutive identical
        pts = [obs[0]]
        for t, r in obs[1:]:
            if r != pts[-1][1]:
                pts.append((t, r))
        bounds = [pd.Timestamp.min] + [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) / 2 for i in range(1, len(pts))] + [pd.Timestamp.max]
        for i, (_, r) in enumerate(pts):
            mm = m & (S.ts >= bounds[i]) & (S.ts < bounds[i + 1])
            new[mm] = 100 + r          # temp namespace
    # slots never anchored by a roll call take a register-free id (or 0 if none is left)
    anchored = {k for k, v in seg.items() if v}
    for k in range(1, n_slots + 1):
        if k not in anchored:
            new[S.cow == k] = 100 + spare.pop(0) if spare else 100
    new = new.where(new < 100, new - 100)
    # exclusivity: at any timestamp a register id appears at most once per camera
    dup = pd.DataFrame({"cam": S.cam, "ts": S.ts, "cow": new}).duplicated(["cam", "ts", "cow"], keep="first") & (new > 0)
    new[dup] = 0
    return new


def reanchor(S, out_dir):
    times = schedule(S)
    obs = run_rollcalls(S, times)
    if obs.empty:
        return S, {"rollcalls": 0}
    o, reg = build_register(obs)
    S = S.copy(); S["tracker_cow"] = S.cow
    S["cow"] = relabel(S, o).values
    o.drop(columns=["sig"]).to_csv(out_dir / "rollcall_observations.csv", index=False)
    reg["cow_id"] = reg.rid.map(lambda k: f"Cow{k:02d}")
    reg.to_csv(out_dir / "herd_register.csv", index=False)
    changed = float((S.cow != S.tracker_cow).mean())
    info = {"rollcalls": len(times), "observations": int(len(obs)), "identifiable_observations": int((o.rid > 0).sum()),
            "register_size": int(len(reg)), "samples_relabelled_share": round(changed, 3)}
    print("reanchor:", info, flush=True)
    return S, info
