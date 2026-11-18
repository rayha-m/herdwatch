"""Identity-consistency measurement with agent roll calls (and marks lookup for risk cards).

Every 30 min in daylight the agent describes each cow visible on Cam 03 (ear-tag colours,
white face markings; pipeline.rollcall / pipeline.reanchor.run_rollcalls, cached in
work/rollcall.jsonl). For pairs of good-quality observations we test attribute
compatibility (white-face status equal and tag colours overlapping):
  * same tracker id, gap g      -> does the tracker still follow the same animal?
  * different ids, same moment  -> chance level of compatibility
The curve (agreement vs gap) is the identity half-life of the tracker.

    python -m pipeline.idcheck --run full
"""
import argparse, itertools, json
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from .config import OUT, WORK
from .reanchor import schedule, run_rollcalls

M = {"pink": "red", "orange": "red"}


def load_obs():
    o = pd.DataFrame([json.loads(l) for l in open(WORK / "rollcall.jsonl")])
    o["ts"] = pd.to_datetime(o.rc_ts, format="ISO8601")
    o["tags"] = o.tag_colours.map(lambda t: frozenset(M.get(c, c) for c in t if c != "none_visible"))
    o["wf"] = o.white_markings.map(lambda w: True if w in ("white_face", "white_blaze_or_patch_on_face") else (False if w == "none" else None))
    return o


def curve(o):
    g = o[(o.visible_quality == "good") & ((o.tags.map(len) > 0) | o.wf.notna())].reset_index(drop=True)

    def agree(a, b):
        if a.wf is not None and b.wf is not None and a.wf != b.wf:
            return False
        if len(a.tags) and len(b.tags) and not (a.tags & b.tags):
            return False
        return True
    rows = []
    for i, j in itertools.combinations(range(len(g)), 2):
        a, b = g.iloc[i], g.iloc[j]
        gap = abs((a.ts - b.ts).total_seconds()) / 3600; same = a.tracker_cow == b.tracker_cow
        if gap == 0 and same:
            continue
        rows.append((same, gap, agree(a, b)))
    P = pd.DataFrame(rows, columns=["same_id", "gap_h", "agree"])
    P["gap"] = pd.cut(P.gap_h, [-0.01, 0.01, 1.1, 4, 12, 24, 48], labels=["same moment", "<=1 h", "1-4 h", "4-12 h", "12-24 h", "24-48 h"])
    C = P.groupby(["gap", "same_id"], observed=True).agree.agg(["mean", "size"]).reset_index()
    return C, len(g)


def marks(o, tracker_cow, ts, max_gap_h=1.0):
    """Visible marks of the animal behind a tracker id near a time (for pen riders)."""
    x = o[(o.tracker_cow == tracker_cow) & (o.visible_quality != "poor")].copy()
    if x.empty:
        return None
    x["gap"] = (x.ts - pd.Timestamp(ts)).abs().dt.total_seconds() / 3600
    x = x[x.gap <= max_gap_h].sort_values("gap")
    if x.empty:
        return None
    r = x.iloc[0]
    tags = (", ".join(sorted(r.tags)) + " ear tag") if r.tags else "ear tag not visible"
    wf = {True: "white face markings", False: "no white face", None: "face not visible"}[r.wf]
    return f"{tags}; {wf}; {r.other_marks} (agent roll call {r.ts:%a %H:%M})"


def run(name):
    out = OUT / "level1" / name
    S = pd.read_parquet(out / "samples.parquet")
    run_rollcalls(S, schedule(S))           # uses the cache; only missing roll calls cost calls
    o = load_obs()
    C, n = curve(o)
    C.to_csv(out / "id_consistency_rollcall.csv", index=False)
    piv = C.pivot(index="gap", columns="same_id", values="mean")
    fig, ax = plt.subplots(figsize=(5.6, 2.6), dpi=140)
    xs = np.arange(len(piv))
    if True in piv:
        ax.plot(xs, piv[True].values, marker="o", color="#2a78d6", lw=2, label="same tracker ID")
    if False in piv:
        ax.plot(xs, piv[False].values, marker="o", color="#8a8984", lw=2, label="different cows (chance)")
    ax.set_xticks(xs); ax.set_xticklabels(piv.index, fontsize=8); ax.set_ylim(0, 0.6)
    ax.set_ylabel("attribute agreement", fontsize=8); ax.legend(fontsize=7, frameon=False)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout(); fig.savefig(out / "id_consistency_rollcall.png"); plt.close(fig)
    print(f"{n} usable observations"); print(C.to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--run", default="full"); a = ap.parse_args(); run(a.run)
