"""Level 1 orchestrator: detections -> posture -> identities -> behaviours -> agent hard
cases -> cow-minute labels, bouts, time budgets, ID-check frames.

    # vertical slice (one camera, one hour)
    python -m pipeline.level1 --cams "Cam 03" --start "2026-11-08 17:00" --hours 1 --name slice_c3_1h --max-cases 10
    # full run
    python -m pipeline.level1 --cams "Cam 01" "Cam 02" "Cam 03" --name full --max-cases 150
"""
import argparse, json, os, shutil, glob
from datetime import timedelta
import numpy as np, pandas as pd, cv2
from .config import WORK, OUT, N_COWS, ZONES
from .posture import load_dets, predict
from .identity import tracklets, assign
from .behavior import sample_labels, minute_labels, bouts
from . import agent as ag
from .llm import BudgetExceeded, calls_used, MAX_CALLS

PALETTE = [(0, 0, 0), (230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200), (245, 130, 48),
           (145, 30, 180), (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212)]


def cow_name(k):
    return f"Cow{int(k):02d}" if k and k > 0 else "unassigned"


def load(cams, start, hours):
    D, Es = [], []
    for cam in cams:
        d, E = load_dets(cam.replace(" ", ""))
        if not len(d):
            continue
        if start:
            s = pd.Timestamp(start); e = s + pd.Timedelta(hours=hours)
            keep = ((d.ts >= s) & (d.ts < e)).values
            d, E = d[keep].reset_index(drop=True), E[keep]
        d["p_stand"] = predict(d, E)
        # physical constraint, Cam 02 only (top-down on the trough: a box in the trough zone is a head
        # in the raised bunk, so she is standing). On Cam 01/03 the bunk zone also covers ground where
        # cows lie under the shade (blind audit), so the posture model decides there.
        if cam == "Cam 02":
            from .behavior import FEED_OV_CAM
            at_bunk = d.bunk_ov >= FEED_OV_CAM["Cam 02"]
            d.loc[at_bunk, "p_stand"] = d.loc[at_bunk, "p_stand"].clip(lower=0.9)
        D.append(d); Es.append(E)
    return D, Es


def exemplars(d, tr):
    """Best daylight crop per cow (largest confident box) for the agent's identity questions."""
    ex = {}
    dd = d.drop(columns=["cow"], errors="ignore").merge(tr[["track", "cow"]], on="track")
    dd = dd[(dd.cow > 0) & (~dd.ir.astype(bool))].copy()
    dd["score"] = dd.conf * np.sqrt(dd.w * dd.h)
    for cow, g in dd.groupby("cow"):
        r = g.loc[g.score.idxmax()]
        ex[int(cow)] = (r.cam, r.ts, [r.x1, r.y1, r.x2, r.y2])
    return ex


def pick_cases(d, tr, ml, ex, max_cases, run):
    """Choose a diverse, capped set of hard cases (identity, ambiguous behaviour, night)."""
    cases = []
    # 1) identity: low-margin tracklets, longest first
    idc = tr[tr.id_hard & (tr.cow > 0)].sort_values("n", ascending=False)
    proto = {}
    for cow, g in tr[tr.cow > 0].groupby("cow"):
        e = np.average(np.stack(g.emb.values), axis=0, weights=g.n.values); proto[int(cow)] = e / (np.linalg.norm(e) + 1e-6)
    idc = idc[idc.ir < 0.5]                      # tags are only readable in daylight colour frames
    for r in idc.head(max(1, max_cases // 3)).itertuples():
        g = d[(d.track == r.track) & (~d.ir.astype(bool))]
        if g.empty:
            continue
        m = g.loc[((g.x2 - g.x1) * (g.y2 - g.y1)).idxmax()]   # largest (closest) view of her
        # candidates = assigned cow + the 2 most similar other cows (appearance prototype)
        sims = sorted(((float(proto[k] @ r.emb), k) for k in proto if k != r.cow and k in ex), reverse=True)
        cands = [r.cow] + [k for _, k in sims[:2]]
        cases.append(dict(case_id=f"{run}_id_{r.track}", kind="identity", cam=r.cam, ts=m.ts,
                          box=[m.x1, m.y1, m.x2, m.y2], bunk_ov=m.bunk_ov, water_ov=m.water_ov,
                          posture_prob_standing=m.p_stand, track=r.track,
                          question=f"Which of the candidate cows ({', '.join(cow_name(c) for c in cands)}) is the boxed cow? "
                                   "Compare tag colour and which ear, white markings, body size/shape. Also label her behaviour.",
                          context=f"tracker assigned {cow_name(r.cow)} with a small margin ({r.id_margin:.2f})",
                          candidates=[(cow_name(c), *ex[c]) for c in cands if c in ex]))
    # 2) ambiguous behaviour minutes: low confidence, drinking, social, night
    ml = ml[ml.n_samples > 0].copy()
    pools = [ml[ml.behavior == "drinking"], ml[ml.behavior == "social_interaction"],
             ml[ml.night & (ml.confidence < 0.7)], ml[(~ml.night) & (ml.confidence < 0.6)]]
    per = max(1, (max_cases - len(cases)) // len(pools))
    seen = set()
    for pool in pools:
        for r in pool.sample(min(len(pool), per), random_state=0).itertuples():
            g = d[(d.cow == r.cow) & (d.ts.dt.floor("min") == r.minute)]
            if g.empty or (r.cow, r.minute) in seen:
                continue
            seen.add((r.cow, r.minute)); m = g.iloc[len(g) // 2]
            cases.append(dict(case_id=f"{run}_bh_{cow_name(r.cow)}_{r.minute:%m%d_%H%M}", kind="behaviour" + ("_night" if r.night else ""),
                              cam=m.cam, ts=m.ts, box=[m.x1, m.y1, m.x2, m.y2], bunk_ov=m.bunk_ov, water_ov=m.water_ov,
                              posture_prob_standing=m.p_stand, cow=r.cow, minute=r.minute,
                              question=f"What is {cow_name(r.cow)} doing during this minute? Tracker says '{r.behavior}' "
                                       f"(sample votes: { {k[2:]: v for k, v in r._asdict().items() if k.startswith('f_') and v == v and v > 0} }).",
                              context=f"{r.n_samples} samples this minute; night={r.night}; posture P(standing)={m.p_stand:.2f}"))
    return cases[:max_cases]


def draw_id_frame(cam, ts, rows, out):
    img = ag.whole_frame(cam, ts)
    if img is None:
        return False
    for r in rows.itertuples():
        col = PALETTE[int(r.cow) % len(PALETTE)] if r.cow > 0 else (128, 128, 128)
        cv2.rectangle(img, (int(r.x1), int(r.y1)), (int(r.x2), int(r.y2)), col[::-1], 3)
        txt = f"{cow_name(r.cow)} {r.s_label}"
        cv2.rectangle(img, (int(r.x1), int(r.y1) - 22), (int(r.x1) + 11 * len(txt), int(r.y1)), (0, 0, 0), -1)
        cv2.putText(img, txt, (int(r.x1) + 2, int(r.y1) - 6), 0, 0.55, (255, 255, 255), 1)
    cv2.rectangle(img, (0, 0), (520, 30), (0, 0, 0), -1)
    cv2.putText(img, f"{cam}  {pd.Timestamp(ts):%Y-%m-%d %H:%M:%S}  (ID check)", (6, 21), 0, 0.65, (255, 255, 255), 1)
    cv2.imwrite(str(out), cv2.resize(img, (960, 720)), [cv2.IMWRITE_JPEG_QUALITY, 82])
    return True


def run(cams, start, hours, name, max_cases, reuse_agent=False):
    out = OUT / "level1" / name; out.mkdir(parents=True, exist_ok=True)
    D, Es = load(cams, start, hours)
    byc, allT = {}, []
    for d, E in zip(D, Es):
        cam = d.cam.iloc[0]
        tr = tracklets(d, E)
        if cam == "Cam 03" or "Cam 03" not in cams:      # Cam 03 is the identity anchor
            tr, slots = assign(tr)
            d = d.merge(tr[["track", "cow", "id_conf", "id_hard"]], on="track", how="left")
        else:
            d["cow"] = 0; d["id_conf"] = 0.0; d["id_hard"] = False
            tr["cow"] = 0; tr["id_conf"] = 0.0; tr["id_margin"] = 9.0; tr["id_hard"] = False
        byc[cam] = d; allT.append(tr)
    if "Cam 03" in byc and len(byc) > 1:
        from .crosscam import link
        byc, xrep = link(byc, out)
        for tr in allT:
            cam = tr.cam.iloc[0]
            if cam != "Cam 03":
                m = byc[cam].groupby("track").cow.first()
                tr["cow"] = tr.track.map(m).fillna(0).astype(int)
        print("cross-camera:", json.dumps(xrep, default=str)[:600])
    S0 = pd.concat(list(byc.values()), ignore_index=True)
    T = pd.concat(allT, ignore_index=True)
    rinfo = {}
    if "Cam 03" in byc and not start and os.environ.get("BV_REANCHOR") == "1":
        # Roll-call re-anchoring (experimental, OFF by default). Tested on the full run: the agent's
        # attribute reads are too sparse/noisy (14/356 legible tag numbers) to build a trustworthy
        # 10-cow register; relabelling changed 68% of samples on weak evidence. Roll calls are used
        # instead as an identity-consistency *measurement* (pipeline.idcheck).
        from .reanchor import reanchor
        S0, rinfo = reanchor(S0, out)
        maj = S0[S0.cow > 0].groupby("track").cow.agg(lambda x: x.value_counts().index[0])
        T["cow"] = T.track.map(maj).fillna(0).astype(int)
    S = sample_labels(S0)
    t0 = pd.Timestamp(start) if start else S.ts.min().floor("D")
    if start:
        t1 = t0 + pd.Timedelta(hours=hours)
    else:   # files spill a minute past midnight; don't invent a third day
        mx = S.ts.max()
        t1 = mx.floor("D") if (mx - mx.floor("D")) < pd.Timedelta(minutes=15) else mx.ceil("D")
    minutes = pd.date_range(t0, t1, freq="min", inclusive="left")
    cows = list(range(1, N_COWS + 1))
    ml0 = minute_labels(S, all_minutes=minutes, cows=cows)

    # ---------------- agent hard cases (capped) ----------------
    ex = exemplars(S, T)
    prior = None
    if reuse_agent and (out / "agent_cases.csv").exists():
        # re-apply decisions the agent already made (no new API calls); identities are deterministic,
        # so track ids and cow-minutes still refer to the same animals
        prior = pd.read_csv(out / "agent_cases.csv", parse_dates=["ts", "minute"])
        cases = []
    else:
        cases = pick_cases(S, T, ml0, ex, max_cases, name)
    results = []
    from concurrent.futures import ThreadPoolExecutor

    def _one(c):
        try:
            return c, *ag.run_case(c)
        except BudgetExceeded:
            return c, None, {"rounds": 0, "error": "budget"}
        except Exception as e:          # one bad case must not kill a 48-h run
            print(f"case {c['case_id']} failed: {str(e)[:200]}", flush=True)
            return c, None, {"rounds": 0, "error": str(e)[:500]}
    with ThreadPoolExecutor(4) as ex:
        outcomes = list(ex.map(_one, cases))
    for c, ans, trace in outcomes:
        results.append(dict(case_id=c["case_id"], kind=c["kind"], cam=c["cam"], ts=c["ts"],
                            cow=c.get("cow"), minute=c.get("minute"), track=c.get("track"),
                            answer=json.dumps(ans) if ans else None, rounds=trace.get("rounds")))
        if ans and c["kind"] == "identity":
            m = ans.get("same_cow_as_candidate", "")
            if m.startswith("Cow") and ans.get("confidence", 0) >= 0.6:
                new = int(m[3:5])
                old = int(T.loc[T.track == c["track"], "cow"].iloc[0])
                if new != old:   # swap the two identities for this tracklet's time span
                    T.loc[T.track == c["track"], "cow"] = new
                    S.loc[S.track == c["track"], "cow"] = new
    if prior is not None:
        for r in prior.itertuples():
            ans = json.loads(r.answer) if isinstance(r.answer, str) else None
            results.append(r._asdict() | {"answer": r.answer})
            if ans and r.kind == "identity" and isinstance(r.track, str):
                m = ans.get("same_cow_as_candidate", "")
                if m.startswith("Cow") and ans.get("confidence", 0) >= 0.6:
                    T.loc[T.track == r.track, "cow"] = int(m[3:5]); S.loc[S.track == r.track, "cow"] = int(m[3:5])
        results = [{k: v for k, v in x.items() if k != "Index"} for x in results]
    R = pd.DataFrame(results)
    am = []
    for r in R.itertuples() if len(R) else []:
        if isinstance(r.answer, str) and pd.notna(r.minute) and str(r.kind).startswith("behaviour"):
            a = json.loads(r.answer)
            am.append(dict(cow=r.cow, minute=r.minute, behavior=a["behavior"], confidence=a["confidence"]))
    am = pd.DataFrame(am)
    ml = minute_labels(S, agent_minutes=am if len(am) else None, all_minutes=minutes, cows=cows)
    ml.insert(0, "cow_id", ml.cow.map(cow_name))
    B = bouts(ml)

    # ---------------- outputs ----------------
    B.to_csv(out / "activity.csv", index=False)
    ml.to_csv(out / "activity_minutes.csv", index=False)
    S[["cam", "file", "t_rel", "ts", "track", "cow", "x1", "y1", "x2", "y2", "p_stand", "s_label", "ir"]].to_parquet(out / "samples.parquet")
    T.drop(columns=["emb"]).to_csv(out / "tracklets.csv", index=False)
    R.to_csv(out / "agent_cases.csv", index=False)
    tb = ml.assign(day=ml.minute.dt.date).pivot_table(index=["cow_id"], columns="behavior", values="minute", aggfunc="count", fill_value=0)
    tb.to_csv(out / "time_budget_minutes.csv")
    vis = tb.drop(columns=["other_not_visible"], errors="ignore")
    (vis.div(vis.sum(1).replace(0, np.nan), axis=0) * 100).round(1).to_csv(out / "time_budget_pct_of_observed.csv")
    # ID-check frames every 30 min per camera
    idd = out / "id_check"; idd.mkdir(exist_ok=True)
    for cam in S.cam.unique():
        for t in pd.date_range(t0, t1, freq="30min", inclusive="left"):
            g = S[(S.cam == cam) & (S.ts >= t) & (S.ts < t + pd.Timedelta(minutes=2))]
            if g.empty:
                continue
            ts = g.ts.min()
            draw_id_frame(cam, ts, g[g.ts == ts], idd / f"{cam.replace(' ', '')}_{t:%Y%m%d_%H%M}.jpg")
    # copy traces
    trd = out / "agent_traces"; trd.mkdir(exist_ok=True)   # traces of reused cases stay in place
    for c in cases:
        p = ag.TRACE_DIR / f"{c['case_id']}.json"
        if p.exists():
            shutil.copy(p, trd / p.name)
            if (ag.TRACE_DIR / c["case_id"]).exists():
                shutil.copytree(ag.TRACE_DIR / c["case_id"], trd / c["case_id"], dirs_exist_ok=True)
    # herd prototypes for Level 3 (per cow, per camera: appearance embedding + colour signature)
    from .identity import COLS
    protos = {}
    for (cam, cow), g in T[(T.cow > 0) & (T.ir < 0.5)].groupby(["cam", "cow"]):
        e = np.average(np.stack(g.emb.values), axis=0, weights=g.n.values); e = e / (np.linalg.norm(e) + 1e-6)
        protos[f"{cam}|{int(cow)}"] = dict(emb=e.astype(float).tolist(), **{c: float(np.average(g[c], weights=g.n)) for c in COLS})
    json.dump(protos, open(out / "herd_prototypes.json", "w"))
    summary = dict(cams=cams, start=str(t0), end=str(t1), samples=len(S), tracklets=len(T),
                   cows_assigned=int(T[T.cow > 0].cow.nunique()), minutes=len(minutes),
                   cow_minutes_observed=int((ml.n_samples > 0).sum()), cow_minutes_total=len(ml),
                   label_counts=ml.behavior.value_counts().to_dict(), source_counts=ml.source.value_counts().to_dict(),
                   agent_cases=len(R), agent_calls_used_total=calls_used(), agent_call_cap=MAX_CALLS, reanchor=rinfo)
    json.dump(summary, open(out / "summary.json", "w"), indent=1, default=str)
    print(json.dumps(summary, indent=1, default=str))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", nargs="+", default=["Cam 03"]); ap.add_argument("--start"); ap.add_argument("--hours", type=float, default=1)
    ap.add_argument("--name", required=True); ap.add_argument("--max-cases", type=int, default=10)
    ap.add_argument("--reuse-agent", action="store_true", help="re-apply the agent decisions saved in agent_cases.csv (no API calls)")
    a = ap.parse_args()
    run(a.cams, a.start, a.hours, a.name, a.max_cases, a.reuse_agent)
