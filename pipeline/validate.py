"""Validation: behaviour accuracy and identity consistency, from blind manual audits.

    python -m pipeline.validate behaviour-sheets --run full --n 60     # writes sheets + blank label file
    python -m pipeline.validate id-sheets --run full                   # one row of 8 crops per cow over 48 h
    python -m pipeline.validate score --run full                       # needs data/labels/behaviour_audit.csv, id_audit.csv

Behaviour audit: stratified random cow-minutes (day/night x the pipeline's labels), each
rendered as 3 frames (start/middle/end of the minute) of that cow from the camera with
the most samples. The auditor labels them WITHOUT seeing the pipeline label; the
pipeline label is joined only at scoring time.
ID audit: for each cow id, 8 crops spread over both days (daylight, largest box); the
auditor marks crops that are NOT the same animal as the majority of the row.
"""
import argparse, json, sys
import numpy as np, pandas as pd, cv2
from .config import OUT, ROOT
from .frames import grab_frame
from .agent import locate

LAB = ROOT / "data" / "labels"


def _crop(cam, ts, box, size=(220, 170), pad=0.15):
    path, t = locate(cam, ts)
    if path is None:
        return np.zeros((size[1], size[0], 3), np.uint8)
    im = grab_frame(path, t, scale_w=1280)
    if im is None:
        return np.zeros((size[1], size[0], 3), np.uint8)
    im = cv2.resize(im, (1280, 960)); x1, y1, x2, y2 = [int(v) for v in box]
    pw, ph = int((x2 - x1) * pad), int((y2 - y1) * pad)
    c = im[max(y1 - ph, 0):y2 + ph, max(x1 - pw, 0):x2 + pw]
    return cv2.resize(c, size) if c.size else np.zeros((size[1], size[0], 3), np.uint8)


def behaviour_sheets(run, n, out_dir, seed=7, tag=""):
    d = OUT / "level1" / run
    ml = pd.read_csv(d / "activity_minutes.csv", parse_dates=["minute"])
    S = pd.read_parquet(d / "samples.parquet")
    obs = ml[ml.n_samples >= 5].copy()
    rng = np.random.default_rng(7)
    per = max(1, n // max(1, obs.groupby(["night", "behavior"]).ngroups))
    smp = obs.groupby(["night", "behavior"], group_keys=False).apply(lambda g: g.sample(min(len(g), per), random_state=seed))
    smp = smp.sample(min(n, len(smp)), random_state=seed + 1).reset_index(drop=True)
    smp["audit_id"] = [f"{tag or 'B'}{i:03d}" for i in range(len(smp))]
    rows = []
    for r in smp.itertuples():
        g = S[(S.cow == r.cow) & (S.ts >= r.minute) & (S.ts < r.minute + pd.Timedelta(minutes=1))]
        cam = g.cam.value_counts().index[0]; g = g[g.cam == cam].sort_values("ts")
        picks = [g.iloc[0], g.iloc[len(g) // 2], g.iloc[-1]]
        tiles = [_crop(cam, p.ts, [p.x1, p.y1, p.x2, p.y2]) for p in picks]
        strip = np.hstack(tiles); cv2.rectangle(strip, (0, 0), (70, 22), (0, 0, 0), -1)
        cv2.putText(strip, r.audit_id, (3, 16), 0, 0.55, (255, 255, 255), 1)
        rows.append(strip)
    out_dir.mkdir(parents=True, exist_ok=True)
    for k in range(0, len(rows), 6):
        cv2.imwrite(str(out_dir / f"behaviour_sheet{tag}_{k//6:02d}.jpg"), np.vstack(rows[k:k + 6]))
    smp[["audit_id", "cow_id", "minute", "night"]].to_csv(LAB / f"behaviour_audit{tag}_keys.csv", index=False)
    print(f"{len(smp)} minutes -> {out_dir}; label them in data/labels/behaviour_audit.csv (audit_id,label)")


def id_sheets(run, out_dir, per_cow=8):
    d = OUT / "level1" / run
    S = pd.read_parquet(d / "samples.parquet")
    # Cam 03 side views (the identity anchor): tag colour/ear and white markings are visible there;
    # Cam 02 top-down crops show only backs and cannot be judged for identity.
    s = S[(S.cow > 0) & (~S.ir.astype(bool)) & (S.cam == "Cam 03")].copy(); s["area"] = (s.x2 - s.x1) * (s.y2 - s.y1)
    rows = []
    for cow in range(1, 11):
        g = s[s.cow == cow]
        if g.empty:
            continue
        bins = pd.qcut(g.ts.rank(method="first"), per_cow, labels=False)
        picks = [gg.loc[gg.area.idxmax()] for _, gg in g.groupby(bins)]
        tiles = []
        for j, p in enumerate(picks):
            t = _crop(p.cam, p.ts, [p.x1, p.y1, p.x2, p.y2], size=(180, 140))
            cv2.rectangle(t, (0, 0), (180, 18), (0, 0, 0), -1)
            cv2.putText(t, f"{j} {pd.Timestamp(p.ts):%a %H:%M} {p.cam[-2:]}", (2, 13), 0, 0.4, (255, 255, 255), 1); tiles.append(t)
        while len(tiles) < per_cow:
            tiles.append(np.zeros_like(tiles[0]))
        lab = np.zeros((140, 70, 3), np.uint8); cv2.putText(lab, f"Cow{cow:02d}", (2, 75), 0, 0.5, (255, 255, 255), 1)
        rows.append(np.hstack([lab] + tiles))
    out_dir.mkdir(parents=True, exist_ok=True)
    for k in range(0, len(rows), 5):
        cv2.imwrite(str(out_dir / f"id_sheet_{k//5}.jpg"), np.vstack(rows[k:k + 5]))
    print("wrote id sheets ->", out_dir)


def score(run):
    d = OUT / "level1" / run; res = {}
    keys = LAB / "behaviour_audit_keys.csv"; labs = LAB / "behaviour_audit.csv"
    if keys.exists() and labs.exists():
        k = pd.read_csv(keys, parse_dates=["minute"]); l = pd.read_csv(labs)
        ml = pd.read_csv(d / "activity_minutes.csv", parse_dates=["minute"])
        j = k.merge(l, on="audit_id").merge(ml[["cow_id", "minute", "behavior", "source"]], on=["cow_id", "minute"])
        j = j[j.label != "unclear"]
        coarse = {"ruminating_lying": "lying", "ruminating_standing": "standing"}
        j["ok"] = j.label == j.behavior
        j["ok_coarse"] = j.label.replace(coarse) == j.behavior.replace(coarse)
        res["behaviour"] = dict(n=int(len(j)), accuracy=round(j.ok.mean(), 3), accuracy_posture_level=round(j.ok_coarse.mean(), 3),
                                day=round(j[~j.night].ok.mean(), 3), night=round(j[j.night].ok.mean(), 3) if j.night.any() else None,
                                by_source=j.groupby("source").ok.agg(["mean", "size"]).round(3).to_dict("index"),
                                confusion=pd.crosstab(j.label, j.behavior).to_dict())
    ida = LAB / "id_audit.csv"
    if ida.exists():
        a = pd.read_csv(ida)
        a = a[a.n_judgeable > 0]
        res["identity"] = dict(cows=int(len(a)), crops_judgeable=int(a.n_judgeable.sum()),
                               consistent_share=round(float(a.n_consistent.sum() / a.n_judgeable.sum()), 3),
                               per_cow=a.set_index("cow_id")[["n_consistent", "n_judgeable"]].to_dict("index"))
    json.dump(res, open(OUT / "validation.json", "w"), indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("--run", default="full"); ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--out", default=str(ROOT / "work" / "audit")); a = ap.parse_args()
    from pathlib import Path
    {"behaviour-sheets": lambda: behaviour_sheets(a.run, a.n, Path(a.out)), "id-sheets": lambda: id_sheets(a.run, Path(a.out)),
     "score": lambda: score(a.run)}[a.cmd]()
