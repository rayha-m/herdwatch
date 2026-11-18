"""Lying vs standing classifier.

Features: ResNet-18 crop embedding (from detect_track) + box geometry
(aspect, height relative to what a standing cow measures at that image row,
foot y). Trained on hand labels in data/labels/posture_labels.csv, evaluated
with grouped cross-validation (groups = track, so near-duplicate frames of one
cow never sit on both sides of the split).

    python -m pipeline.posture train      # fits + writes work/posture_model.pkl and CV report
"""
import sys, glob, json, pickle
import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.metrics import classification_report, confusion_matrix
from .config import WORK, ROOT

MODEL = WORK / "posture_model.pkl"
GEOM = ["aspect", "h_rel", "w_rel", "fy_n", "fx_n", "ir"]


def load_dets(cam_dir):
    """All detections + embeddings for a camera dir, aligned by row."""
    dfs, es = [], []
    for f in sorted(glob.glob(str(WORK / "det" / cam_dir / "*.parquet"))):
        d = pd.read_parquet(f); e = np.load(f[:-8] + ".npy")
        if len(d) != len(e):
            continue
        dfs.append(d); es.append(e)
    if not dfs:
        return pd.DataFrame(), np.zeros((0, 512), np.float32)
    return pd.concat(dfs, ignore_index=True), np.concatenate(es)   # float16; cast per chunk


def add_geom(d):
    """Height of a standing cow grows roughly linearly with foot row (perspective).
    Fit per camera: median box height as a function of fy, use ratio as feature."""
    d = d.copy()
    for cam, g in d.groupby("cam"):
        bins = np.clip((g.fy // 60).astype(int), 0, 16)
        ref_h = g.groupby(bins).h.quantile(0.8).reindex(range(17)).interpolate().bfill().ffill()
        ref_w = g.groupby(bins).w.quantile(0.8).reindex(range(17)).interpolate().bfill().ffill()
        d.loc[g.index, "h_rel"] = g.h.values / ref_h.loc[bins].values
        d.loc[g.index, "w_rel"] = g.w.values / ref_w.loc[bins].values
    d["fy_n"] = d.fy / 960; d["fx_n"] = d.fx / 1280; d["ir"] = d.ir.astype(float)
    return d


def featurize(d, E):
    g = add_geom(d)[GEOM].fillna(1).values
    return np.hstack([E.astype(np.float32), g * 3.0])          # upweight geometry vs 512-d embedding


def _join_labels():
    lab = pd.read_csv(ROOT / "data/labels/posture_labels.csv", parse_dates=["ts"])
    lab = lab[lab.posture != "unclear"]
    X, y, groups, irs = [], [], [], []
    for cam, lg in lab.groupby("cam"):
        d, E = load_dets(cam.replace(" ", ""))
        Xa = featurize(d, E)
        key = d.file + "|" + d.t_rel.round(2).astype(str) + "|" + d.x1.astype(str) + "|" + d.y1.astype(str)
        pos = dict(zip(key, range(len(d))))
        for r in lg.itertuples():
            k = f"{r.file}|{round(r.t_rel,2)}|{r.x1}|{r.y1}"
            if k in pos:
                X.append(Xa[pos[k]]); y.append(int(r.posture == "standing")); groups.append(r.track); irs.append(bool(r.ir))
    return np.array(X), np.array(y), np.array(groups), np.array(irs)


def train():
    X, y, groups, irs = _join_labels()
    clf = make_pipeline(StandardScaler(), LogisticRegression(C=0.05, max_iter=3000, class_weight="balanced"))
    k = min(5, len(set(groups)))
    pred = cross_val_predict(clf, X, y, groups=groups, cv=GroupKFold(k))
    rep = classification_report(y, pred, target_names=["lying", "standing"], output_dict=True)
    cm = confusion_matrix(y, pred).tolist()
    clf.fit(X, y)
    pickle.dump(clf, open(MODEL, "wb"))
    out = {"n": int(len(y)), "n_lying": int((y == 0).sum()), "n_standing": int((y == 1).sum()),
           "grouped_cv_folds": k, "confusion[lying,standing]": cm,
           "accuracy": rep["accuracy"], "lying_f1": rep["lying"]["f1-score"], "standing_f1": rep["standing"]["f1-score"],
           "accuracy_day": float((pred[~irs] == y[~irs]).mean()), "n_day": int((~irs).sum()),
           "accuracy_night_ir": float((pred[irs] == y[irs]).mean()), "n_night_ir": int(irs.sum())}
    (ROOT / "outputs").mkdir(exist_ok=True)
    json.dump(out, open(ROOT / "outputs/posture_model_cv.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


def predict(d, E, chunk=200_000):
    clf = pickle.load(open(MODEL, "rb"))
    g = add_geom(d)[GEOM].fillna(1).values * 3.0
    out = np.empty(len(d))
    for i in range(0, len(d), chunk):
        X = np.hstack([E[i:i + chunk].astype(np.float32), g[i:i + chunk]])
        out[i:i + chunk] = clf.predict_proba(X)[:, 1]
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["train"]:
        train()
