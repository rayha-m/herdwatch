"""Evidence clips + agent clinical-sign check + one-page risk cards for the top-N cows.

For each top cow, clips are chosen to show *her* top reasons:
  low bunk time / weak response to fresh feed -> during a feed delivery, wide view (herd at bunk)
  lying / long lying bouts                    -> middle of her longest daytime lying bout
  isolation                                   -> the frame where she is farthest from herd-mates
  always                                      -> best daylight close-up (also used for the clinical check)
Clips are 12 s, 960 px wide, H.264, with her box drawn at the clip's start position.

    python -m pipeline.evidence --run full --top 3
"""
import argparse, json, subprocess
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from .config import OUT, W
from .frames import real_path
from . import agent as ag
from .llm import BudgetExceeded

CLIP_S = 12
FEATS = ["feeding_min_per_day", "bunk_visits_per_day", "feedcall_attendance", "lying_pct",
         "longest_lying_bout_min", "isolation_nn_bw", "drinking_visits_per_day"]


def clip(cam, ts, box, out, label):
    path, t = ag.locate(cam, ts)
    if path is None:
        return None
    t0 = max(t - CLIP_S / 2, 0)
    x1, y1, x2, y2 = [int(v * 960 / W) for v in box]
    vf = (f"scale=960:-2,fps=15,drawbox=x={x1}:y={y1}:w={x2-x1}:h={y2-y1}:color=yellow@0.9:t=3")
    cmd = ["ffmpeg", "-loglevel", "fatal", "-y", "-ss", f"{t0:.2f}", "-c:v", "hevc", "-i", real_path(path),
           "-t", str(CLIP_S), "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd)
    return out if r.returncode == 0 and out.exists() else None


def pick_moments(cow, S, ml, events, reasons):
    ml = ml.copy(); ml["minute"] = pd.to_datetime(ml.minute)
    """Return list of (why, cam, ts, box) for this cow."""
    k = int(cow[3:])
    s = S[(S.cow == k) & (~S.ir.astype(bool))].copy()
    if s.empty:
        s = S[S.cow == k].copy()
    s["area"] = (s.x2 - s.x1) * (s.y2 - s.y1)
    picks = []
    m = ml[ml.cow_id == cow]
    # 1) during a feed delivery, not feeding herself (if that is a reason)
    if any(w in reasons for w in ("bunk", "fresh feed")) and events:
        herd = pd.read_csv(OUT / "level2" / "herd_feeding_share_by_minute.csv", parse_dates=["minute"]).set_index("minute").herd_feeding_share
        for e in events:
            w = m[(m.minute >= e) & (m.minute < e + pd.Timedelta(minutes=45)) & (m.behavior != "feeding") & (m.behavior != "other_not_visible")]
            w = w.assign(hs=w.minute.map(herd)).dropna(subset=["hs"])
            w = w[w.hs >= 0.5]                      # the herd really is at the bunk then
            if len(w):
                mm = w.loc[w.hs.idxmax(), "minute"]
                g = s[(s.ts >= mm) & (s.ts < mm + pd.Timedelta(minutes=1)) & (s.cam == "Cam 03")]
                if len(g):
                    r = g.iloc[0]; hs = w.hs.max()
                    picks.append((f"after a feed delivery ({e:%a %H:%M}) the pipeline labels {hs*100:.0f}% of visible pen-mates as feeding (mostly seen on the bunk cameras, Cam 02/01) while {cow} is {w.loc[w.hs.idxmax(), 'behavior']} (wide Cam 03 view)", r.cam, r.ts, [r.x1, r.y1, r.x2, r.y2])); break
    # 2) longest daytime lying bout
    if "lying" in reasons:
        ly = m[m.behavior.isin(["lying", "ruminating_lying"])].sort_values("minute")
        if len(ly):
            b = (ly.minute.diff() != pd.Timedelta(minutes=1)).cumsum()
            best = ly.groupby(b).minute.agg(["min", "max", "size"]).sort_values("size", ascending=False)
            for _, bb in best.iterrows():
                mid = bb["min"] + (bb["max"] - bb["min"]) / 2
                g = s[(s.ts >= mid - pd.Timedelta(minutes=2)) & (s.ts <= mid + pd.Timedelta(minutes=2))]
                if len(g):
                    r = g.sort_values("area").iloc[-1]
                    mm_ = ml[(ml.minute == mid.floor("min")) & (ml.behavior != "other_not_visible") & (ml.cow_id != cow)]
                    herd_ly = mm_.behavior.isin(["lying", "ruminating_lying"]).mean() if len(mm_) else float("nan")
                    ctx = (f"; {herd_ly*100:.0f}% of visible pen-mates were also lying then" + (" (herd rest period: weak evidence on its own)" if herd_ly >= 0.6 else " (she is lying while most pen-mates are up)")) if herd_ly == herd_ly else ""
                    picks.append((f"lying bout of {int(bb['size'])} min ({bb['min']:%a %H:%M}-{bb['max']:%H:%M}){ctx}", r.cam, r.ts, [r.x1, r.y1, r.x2, r.y2])); break
    # 3) isolation
    if "herd-mate" in reasons:
        c3 = S[(S.cam == "Cam 03") & (~S.ir.astype(bool)) & (S.cow > 0)]
        c3 = c3[(c3.x1 > 5) & (c3.x2 < 1275) & (c3.y2 < 955) & ((c3.x2 - c3.x1) >= 40)]   # whole, reasonably large boxes
        best, bd = None, 0
        for ts, g in c3[c3.ts.isin(s[s.cam == "Cam 03"].ts.unique()[::15])].groupby("ts"):
            if len(g) < 5 or k not in g.cow.values:
                continue
            P = np.c_[(g.x1 + g.x2) / 2, g.y2]; me = np.where(g.cow.values == k)[0][0]
            d = np.delete(np.linalg.norm(P - P[me], axis=1), me).min() / max(g.x2.values[me] - g.x1.values[me], 20)
            if d > bd:
                bd, best = d, g.iloc[me]
        if best is not None:
            picks.append((f"standing apart: nearest herd-mate {bd:.1f} body widths away", best.cam, best.ts, [best.x1, best.y1, best.x2, best.y2]))
    # 4) best close-up (largest daylight box, prefer front cameras)
    g = s[s.cam.isin(["Cam 01", "Cam 02"])]
    g = g if len(g) else s
    if len(g):
        r = g.sort_values("area").iloc[-1]
        picks.append(("best close-up (used for the clinical-sign check)", r.cam, r.ts, [r.x1, r.y1, r.x2, r.y2]))
    return picks[:3] if len(picks) > 3 else picks


def clinical_check(cow, cam, ts, box, run):
    case = dict(case_id=f"{run}_clinical_{cow}", kind="clinical", cam=cam, ts=ts, box=box, bunk_ov=0, water_ov=0,
                posture_prob_standing=0.5,
                question=(f"This is {cow}, ranked high for BRD risk from behaviour. Examine her for VISIBLE clinical signs of "
                          "bovine respiratory disease: ear carriage (drooped/ears down), head carried low, nasal or ocular "
                          "discharge, laboured or open-mouth breathing, gaunt flank, dull or isolated demeanour. Zoom in on the head "
                          "and look at a few time offsets. Report only what you can actually see; say not_assessable if resolution "
                          "does not allow it. Also label her behaviour."),
                context="clinical-sign check for a risk card")
    try:
        ans, _ = ag.run_case(case)
    except Exception as e:      # budget reached or API unavailable: the card says so instead of failing
        ans = {"clinical_signs": ["not_assessed"], "confidence": 0.0,
               "evidence": f"Agent check not run ({str(e)[:120]}). Re-run `python -m pipeline.evidence --run full --top 3` when the API is available."}
    return ans


def card(cow, R, F, ml, moments, clips, clin, out_dir, find=None):
    r = R.set_index("cow_id").loc[cow]
    herd_med = R.set_index("cow_id")[FEATS].median()
    # chart 1: feature vs herd (as % of herd median)
    fig, ax = plt.subplots(figsize=(6.2, 2.6), dpi=130)
    labs = {"feeding_min_per_day": "Bunk time", "bunk_visits_per_day": "Bunk visits", "feedcall_attendance": "Fresh-feed response",
            "lying_pct": "Lying %", "longest_lying_bout_min": "Longest lying bout", "isolation_nn_bw": "Distance to herd-mate",
            "drinking_visits_per_day": "Waterer visits"}
    vals = [(labs[f], (r[f] / herd_med[f] * 100) if herd_med[f] else np.nan) for f in FEATS if pd.notna(r.get(f))]
    ax.barh([v[0] for v in vals][::-1], [v[1] for v in vals][::-1], color="#2a78d6", height=0.55)
    ax.axvline(100, color="#8a8984", lw=1); ax.set_xlabel("% of herd median (100 = typical)")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(labelsize=8); fig.tight_layout(); fig.savefig(out_dir / f"{cow}_features.png"); plt.close(fig)
    # chart 2: her actogram vs herd feeding
    m = ml[ml.cow_id == cow].set_index("minute").behavior
    colors = {"feeding": "#2a78d6", "walking": "#eb6834", "drinking": "#1baf7a", "standing": "#eda100", "social_interaction": "#e87ba4",
              "grooming": "#e87ba4", "ruminating_standing": "#008300", "ruminating_lying": "#008300", "lying": "#6250d6", "other_not_visible": "#ebeae6"}
    fig, ax = plt.subplots(figsize=(6.2, 0.9), dpi=130)
    t0 = m.index.min()
    xs = ((m.index - t0).total_seconds() / 3600).values
    ax.bar(xs, 1, width=1 / 60, color=[colors.get(b, "#ebeae6") for b in m.values], align="edge")
    ax.set_yticks([]); ax.set_xlim(0, xs.max() + 1 / 60); ax.set_xlabel(f"hours from {t0:%a %d %b %H:%M}", fontsize=8)
    ax.tick_params(labelsize=7)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout(); fig.savefig(out_dir / f"{cow}_actogram.png"); plt.close(fig)
    clin_txt = ("**not run**: the agent API was unavailable (account credit exhausted) during the final run. "
                "Re-run `python -m pipeline.evidence --run full --top 3` to add it.")
    if clin:
        signs = ", ".join(clin.get("clinical_signs", [])) or "none"
        clin_txt = f"**{signs}** (confidence {clin.get('confidence', 0):.2f}). {clin.get('evidence', '')}"
    lines = [f"# Risk card: {cow}", "",
             f"**BRD risk score {r.risk_score:.2f}** (rank {int(r['rank'])} of {len(R)}; 0.5 = herd-typical) · data reliability: **{r.reliability}** "
             f"({r.observed_hours:.0f} h observed across both days, mean label confidence {r.mean_label_confidence:.2f})", "",
             "**Why she is flagged (behaviour observed by the cameras, compared with her 9 pen-mates):**", ""]
    lines += [f"- {x.strip()}" for x in str(r.top_reasons).split(";")]
    lines += ["", f"![features vs herd]({cow}_features.png)", "",
              "**48-hour activity** (purple = lying, blue = feeding, yellow = standing, aqua = drinking, grey = not visible):", "",
              f"![actogram]({cow}_actogram.png)", "",
              f"**Visible clinical signs (agent check on best close-up, estimated):** {clin_txt}", "",
              "**How to find her in the pen** (her marks at the time of each clip, from the agent's roll call within 1 h; "
              "IDs are reliable for about an hour, see README > Validation): " + ("; ".join(find) if find else "no roll-call description within 1 h of the clips"), "",
              "**Video evidence:**", ""]
    for (why, cam, ts, _), c in zip(moments, clips):
        if c:
            lines.append(f"- [{c.name}](../../evidence_clips/{c.name}): {why}. {cam}, {pd.Timestamp(ts):%a %d %b %H:%M:%S}")
    lines += ["", "**Suggested pen-rider action:** pull and examine (rectal temperature ≥ 40.0 °C / 104 °F, respiratory rate and "
              "character, nasal and ocular discharge, ear carriage, lung auscultation). Compare with a herd-mate.", "",
              "_What this card is and is not:_ behaviour is **observed** (YOLO + tracker + agent on hard cases). Identity and minute "
              "labels carry error (see README, Validation). The risk score is **estimated** relative to this herd over 2 days. "
              "No clinical records were available, so it is a screening flag, not a diagnosis."]
    (out_dir / f"{cow}.md").write_text("\n".join(lines))


def run(name, top):
    l1 = OUT / "level1" / name; l2 = OUT / "level2"
    R = pd.read_csv(l2 / "brd_risk.csv"); F = pd.read_csv(l2 / "features_per_cow_day.csv")
    ml = pd.read_csv(l1 / "activity_minutes.csv", parse_dates=["minute"])
    S = pd.read_parquet(l1 / "samples.parquet")
    events = [pd.Timestamp(e) for e in json.load(open(l2 / "risk_model.json"))["feed_events"]]
    cdir = OUT / "evidence_clips"; cdir.mkdir(parents=True, exist_ok=True)
    rdir = l2 / "risk_cards"; rdir.mkdir(parents=True, exist_ok=True)
    summary = []
    for cow in R.sort_values("rank").cow_id.head(top):
        reasons = R.set_index("cow_id").at[cow, "top_reasons"]
        moments = pick_moments(cow, S, ml, events, reasons)
        clips = []
        for i, (why, cam, ts, box) in enumerate(moments):
            out = cdir / f"{cow}_{i+1}_{cam.replace(' ', '')}_{pd.Timestamp(ts):%m%d_%H%M%S}.mp4"
            clips.append(clip(cam, ts, box, out, cow))
        close = [mm for mm in moments if mm[0].startswith("best close-up")] or moments[-1:]
        clin = clinical_check(cow, *close[0][1:], name) if close else None
        find = []
        try:
            from .idcheck import load_obs, marks
            o = load_obs()
            for why, cam, ts, _ in moments:
                m = marks(o, int(cow[3:]), ts)
                if m and m not in find:
                    find.append(m)
        except Exception as e:
            print("marks lookup failed:", e)
        card(cow, R, F, ml, moments, clips, clin, rdir, find)
        summary.append(dict(cow_id=cow, clips=[c.name for c in clips if c], clinical=clin))
        print(cow, [c.name for c in clips if c], (clin or {}).get("clinical_signs"))
    json.dump(summary, open(l2 / "evidence_summary.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--run", default="full"); ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args(); run(a.run, a.top)
