"""Level 3: live early warning on new video.

    python -m pipeline.live --clip "/path/to/Event20261108171045003.Avi"      # one clip
    python -m pipeline.live --watch /path/to/incoming --poll 30               # keep watching a folder

For each new clip (any of the three cameras; camera inferred from the filename
suffix 006/017/003 or --cam):
  1. detect + track + posture (same models as Level 1)
  2. identity: Hungarian matching of tracklets to the herd prototypes saved by the
     Level 1 run (appearance + tag colour), one cow per track at a time
  3. behaviour per minute (same rules as Level 1)
  4. risk update: prior = Level 2 risk; live evidence = how far the cow deviates from
     her pen-mates *in this clip* (lying or away while the herd feeds, isolation,
     low activity).  logit(risk) += 0.8 * live_z, capped.
  5. alert only with evidence in this clip: lying for most of the clip (>= 3-10 min) while
     >= 60 % of visible pen-mates feed, or updated risk >= 0.70 together with a clearly
     abnormal clip (live deviation >= 1). At most 3 alerts per clip. The alert runs ONE agent clinical-sign check
     (ears, head carriage, nasal/ocular discharge, breathing), cuts an evidence clip
     and writes outputs/level3/alerts/<time>_<cow>.md + .json. If BV_ALERT_WEBHOOK is
     set, the JSON is POSTed there (e.g. an SMS/Slack bridge); off by default.
State (latest risk per cow) persists in outputs/level3/live_state.json so risk carries
over between clips.
"""
import argparse, json, os, re, time, urllib.request
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import numpy as np, pandas as pd
from ultralytics import YOLO
from .config import OUT, WORK, WEIGHTS, YOLO_WEIGHTS, N_COWS
from .detect_track import process_video, Embedder
from .posture import predict
from .identity import tracklets, cost, Slot, COLS
from .behavior import sample_labels, minute_labels, FEED_OV, FEED_OV_CAM
from .evidence import clip as cut_clip
from . import agent as ag
from .llm import BudgetExceeded

SUFFIX_CAM = {"006": "Cam 01", "017": "Cam 02", "003": "Cam 03"}
L3 = OUT / "level3"
ALERT_RISK = 0.70
MAX_ALERTS = 3          # per clip: a pen rider acts on a short list, not on every cow


def cam_from_name(p):
    m = re.search(r"Event(\d{14})(\d{3})\.avi$", Path(p).name, re.I)
    return (SUFFIX_CAM.get(m.group(2)), pd.to_datetime(m.group(1), format="%Y%m%d%H%M%S")) if m else (None, None)


def load_protos(run):
    p = OUT / "level1" / run / "herd_prototypes.json"
    return json.load(open(p)) if p.exists() else {}


def identify(tr, protos, cam):
    """Assign tracklets to the 10 known cows using prototypes for this camera (fallback: Cam 03)."""
    slots = []
    for k in range(1, N_COWS + 1):
        p = protos.get(f"{cam}|{k}") or protos.get(f"Cam 03|{k}")
        if not p:
            continue
        r = SimpleNamespace(emb=np.array(p["emb"], np.float32), n=50, t1=pd.Timestamp.min, x1=0, y1=0, ir=0.0,
                            **{c: p.get(c, 0.0) for c in COLS})
        s = Slot(k, r); s.t = None; slots.append(s)
    lab = {}
    tr = tr.sort_values("t0")
    busy = {}
    for t0, g in tr.groupby(tr.t0.dt.floor("2s")):
        free = [s for s in slots if busy.get(s.k, pd.Timestamp.min) < g.t0.min()]
        if not free:
            continue
        def c(s, r):   # appearance + colour only (no motion prior across clips)
            e = 1 - float(s.emb @ r.emb)
            from .identity import colour_vec
            col = float(np.abs(s.col - colour_vec(r)).sum()) / len(COLS) * (0.3 if r.ir > 0.5 else 1)
            return 2 * e + col
        rows = list(g.itertuples())
        C = np.array([[c(s, r) for s in free] for r in rows])
        from scipy.optimize import linear_sum_assignment
        ri, ci = linear_sum_assignment(C)
        for i, j in zip(ri, ci):
            lab[rows[i].track] = free[j].k; busy[free[j].k] = rows[i].t1
    tr["cow"] = tr.track.map(lab).fillna(0).astype(int)
    return tr


def live_deviation(ml, S):
    """Per cow z-like deviation from pen-mates in this clip (positive = more BRD-like)."""
    obs = ml[ml.behavior != "other_not_visible"]
    if obs.empty:
        return pd.DataFrame()
    herd_feed = obs.groupby("minute").behavior.apply(lambda s: (s == "feeding").mean())
    rows = []
    for cow, g in obs.groupby("cow_id"):
        lying = g.behavior.isin(["lying", "ruminating_lying"])
        hf = herd_feed.reindex(g.minute).values
        lying_while_herd_feeds = int((lying.values & (hf >= 0.6)).sum())
        rows.append(dict(cow_id=cow, minutes=len(g), feeding=(g.behavior == "feeding").mean(), lying=lying.mean(),
                         lying_while_herd_feeds=lying_while_herd_feeds))
    D = pd.DataFrame(rows).set_index("cow_id")
    z = pd.Series(0.0, index=D.index)
    for col, sgn in (("feeding", -1), ("lying", 1)):
        x = D[col]; sd = x.std() if x.std() > 1e-6 else 1.0
        z += sgn * (x - x.median()) / sd
    z += D.lying_while_herd_feeds / 10.0
    D["live_z"] = z.clip(-3, 3).round(2)
    return D


def process_clip(path, cam=None, run="full", model=None, emb=None, use_agent=True):
    L3.mkdir(parents=True, exist_ok=True); (L3 / "alerts").mkdir(exist_ok=True)
    cam_n, start = cam_from_name(path)
    cam = cam or cam_n
    if cam is None:
        raise SystemExit("camera unknown: pass --cam")
    row = SimpleNamespace(cam=cam, day=f"live", file=Path(path).name, path=str(path), start=start or pd.Timestamp.now().floor("s"))
    from .index_videos import avi_duration
    with open(path, "rb") as fh:
        dur = avi_duration(fh.read(4096))
    ag.register_live(cam, row.start, dur if dur == dur else 600, path)
    t = time.time()
    model = model or YOLO(str(WEIGHTS / YOLO_WEIGHTS)); emb = emb or Embedder()
    tmp = WORK / "live"; process_video(row, model, emb, tmp)
    d = pd.read_parquet(tmp / (row.file[:-4] + ".parquet")); E = np.load(tmp / (row.file[:-4] + ".npy"))
    if d.empty:
        print(f"{row.file}: no cattle detected (night IR / empty view)"); return []
    d["p_stand"] = predict(d, E)
    if cam == "Cam 02":   # head-in-trough implies standing only on the top-down bunk camera
        at_bunk = d.bunk_ov >= FEED_OV_CAM["Cam 02"]; d.loc[at_bunk, "p_stand"] = d.loc[at_bunk, "p_stand"].clip(lower=0.9)
    tr = identify(tracklets(d, E), load_protos(run), cam)
    d = d.merge(tr[["track", "cow"]], on="track")
    S = sample_labels(d)
    ml = minute_labels(S)
    ml["cow_id"] = ml.cow.map(lambda k: f"Cow{int(k):02d}")
    dev = live_deviation(ml, S)
    # prior risk
    state_p = L3 / "live_state.json"
    state = json.load(open(state_p)) if state_p.exists() else {}
    prior = {}
    rp = OUT / "level2" / "brd_risk.csv"
    if rp.exists():
        prior = pd.read_csv(rp).set_index("cow_id").risk_score.to_dict()
    alerts = []
    for cow, r in dev.iterrows():
        p0 = state.get(cow, {}).get("risk", prior.get(cow, 0.5))
        lg = np.log(p0 / (1 - p0)) + 0.8 * r.live_z * min(r.minutes / 5, 1.0)
        p1 = float(1 / (1 + np.exp(-np.clip(lg, -4, 4))))
        state[cow] = dict(risk=round(p1, 3), updated=str(row.start), last_clip=row.file, live_z=float(r.live_z))
        # an alert needs evidence in THIS clip: a strong trigger, or high risk together with an abnormal clip
        trigger = r.lying_while_herd_feeds >= min(10, max(3, 0.5 * r.minutes))
        if trigger or (p1 >= ALERT_RISK and r.live_z >= 1.0):
            alerts.append((cow, p0, p1, r, trigger))
    json.dump(state, open(state_p, "w"), indent=1)
    out = []
    for cow, p0, p1, r, trigger in sorted(alerts, key=lambda a: (-int(bool(a[4])), -a[2]))[:MAX_ALERTS]:
        k = int(cow[3:]); g = S[S.cow == k].copy(); g["area"] = (g.x2 - g.x1) * (g.y2 - g.y1)
        best = g.sort_values("area").iloc[-1]
        stamp = f"{pd.Timestamp(best.ts):%Y%m%d_%H%M%S}"
        clipf = cut_clip(cam, best.ts, [best.x1, best.y1, best.x2, best.y2], L3 / "alerts" / f"{stamp}_{cow}.mp4", cow) if ag.locate(cam, best.ts)[0] else None
        clin = None
        if use_agent:
            try:
                case = dict(case_id=f"live_{stamp}_{cow}", kind="clinical_live", cam=cam, ts=best.ts,
                            box=[best.x1, best.y1, best.x2, best.y2], bunk_ov=float(best.bunk_ov), water_ov=float(best.water_ov),
                            posture_prob_standing=float(best.p_stand),
                            question=(f"LIVE ALERT CHECK for {cow}. Look for visible signs of respiratory disease: drooped ears, head "
                                      "carried low, nasal or ocular discharge, laboured breathing, gaunt flank, isolation. Also label her "
                                      "behaviour. Only report what you can see."),
                            context=f"live risk {p0:.2f}->{p1:.2f}; lying while herd feeds: {int(r.lying_while_herd_feeds)} min")
                clin, _ = ag.run_case(case)
            except (BudgetExceeded, Exception) as e:
                clin = {"error": str(e)[:200]}
        why = []
        if r.lying_while_herd_feeds:
            why.append(f"lying {int(r.lying_while_herd_feeds)} min while >= 60% of pen-mates were feeding")
        why.append(f"feeding {r.feeding*100:.0f}% / lying {r.lying*100:.0f}% of the {int(r.minutes)} observed minutes in this clip")
        signs = ", ".join((clin or {}).get("clinical_signs", []) or ["not checked"])
        msg = (f"PEN 6 ALERT {pd.Timestamp(best.ts):%a %H:%M} {cow}: risk {p0:.2f} -> {p1:.2f}. " + "; ".join(why) +
               f". Visible signs (agent): {signs}. Check temp, ears, nose, breathing. Clip: {clipf.name if clipf else 'n/a'}")
        rec = dict(time=str(best.ts), camera=cam, cow_id=cow, risk_before=round(p0, 3), risk_after=round(p1, 3),
                   trigger="lying while herd feeds" if trigger else "risk threshold", reasons=why, clinical=clin,
                   clip=str(clipf) if clipf else None, message=msg, source_clip=row.file)
        (L3 / "alerts" / f"{stamp}_{cow}.json").write_text(json.dumps(rec, indent=1, default=str))
        (L3 / "alerts" / f"{stamp}_{cow}.md").write_text(
            f"# {msg}\n\n- camera: {cam}\n- source clip: {row.file}\n- evidence clip: {clipf.name if clipf else 'n/a'}\n"
            f"- agent evidence: {(clin or {}).get('evidence', 'n/a')}\n- agent self-check: {(clin or {}).get('self_check', 'n/a')}\n")
        hook = os.environ.get("BV_ALERT_WEBHOOK")
        if hook:
            try:
                urllib.request.urlopen(urllib.request.Request(hook, data=json.dumps(rec, default=str).encode(),
                                                              headers={"Content-Type": "application/json"}), timeout=10)
            except Exception as e:
                print("webhook failed:", e)
        print(msg); out.append(rec)
    dev.to_csv(L3 / f"live_{Path(row.file).stem}.csv")
    print(f"{row.file}: {len(dev)} cows seen, {len(out)} alert(s), {time.time()-t:.0f}s")
    return out


def watch(folder, poll, cam, run):
    seen = set(); model = YOLO(str(WEIGHTS / YOLO_WEIGHTS)); emb = Embedder()
    print(f"watching {folder} every {poll}s (Ctrl-C to stop)")
    while True:
        for p in sorted(Path(folder).glob("*.[Aa]vi")):
            if p in seen or time.time() - p.stat().st_mtime < 10:     # skip files still being written
                continue
            seen.add(p); process_clip(p, cam, run, model, emb)
        time.sleep(poll)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip"); ap.add_argument("--watch"); ap.add_argument("--poll", type=int, default=30)
    ap.add_argument("--cam"); ap.add_argument("--run", default="full"); ap.add_argument("--no-agent", action="store_true")
    a = ap.parse_args()
    if a.watch:
        watch(a.watch, a.poll, a.cam, a.run)
    else:
        process_clip(a.clip, a.cam, a.run, use_agent=not a.no_agent)
