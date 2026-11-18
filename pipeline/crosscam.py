"""Cross-camera identity: carry Cam 03 (whole-pen anchor) identities to Cam 01 and Cam 02.

Cam 01 -> Cam 03: a 2-D mapping of foot points between the two images. Initialised
from hand-picked shared landmarks (waterer, pen corners, bunk ends), then refined
ICP-style: in near-synchronous daytime frames (|dt| <= 1 s) project Cam 01 foot
points, Hungarian-match them to Cam 03 foot points under a distance gate, and refit
a 2nd-order polynomial mapping with RANSAC on all matched pairs. Repeat 4 times.

Cam 02 -> Cam 03: Cam 02 only sees the feed bunk, so the mapping is 1-D: position
along the bunk (Cam 02 box-centre x) -> Cam 03 foot x among cows in the bunk zone,
fitted the same way (robust linear fit, sign learned from the data).

Each Cam 01 / Cam 02 tracklet then takes the majority Cam 03 cow of its matched
detections (vote share = cross-camera ID confidence). Unmatched tracklets stay
unassigned (cow = 0) rather than guessing.
"""
import json
import numpy as np, pandas as pd
from scipy.optimize import linear_sum_assignment
from sklearn.linear_model import RANSACRegressor, LinearRegression
from sklearn.preprocessing import PolynomialFeatures
from sklearn.pipeline import make_pipeline
from .config import WORK

# landmarks: Cam 01 foot-point (x,y) -> Cam 03 foot-point (x,y), 1280x960 coords
LANDMARKS_C1_C3 = [
    ((915, 330), (480, 548)),    # waterer (yellow tank)
    ((560, 175), (40, 520)),     # back-left (west/chute side, far end for Cam 01)
    ((860, 170), (5, 505)),      # back-right (east)
    ((40, 840), (1000, 545)),    # front-left: bunk end at the chute fence
    ((1240, 780), (705, 500)),   # front-right: bunk east end
    ((640, 450), (560, 640)),    # pen centre (approx.)
]


class PolyMap:
    def __init__(self, deg=2):
        self.models = [make_pipeline(PolynomialFeatures(deg), LinearRegression()) for _ in range(2)]

    def fit(self, src, dst):
        for k in range(2):
            self.models[k].fit(src, dst[:, k])
        return self

    def __call__(self, pts):
        pts = np.asarray(pts, float).reshape(-1, 2)
        return np.stack([m.predict(pts) for m in self.models], 1)


def _sync_pairs(a, b, max_dt=1.0):
    """Pair frames of camera a with the nearest frame of camera b (|dt|<=max_dt)."""
    ta = np.sort(a.ts.unique()); tb = np.sort(b.ts.unique())
    if not len(ta) or not len(tb):
        return []
    j = np.searchsorted(tb, ta).clip(1, len(tb) - 1)
    near = np.where(np.abs(tb[j] - ta) < np.abs(tb[j - 1] - ta), tb[j], tb[j - 1])
    ok = np.abs((near - ta).astype("timedelta64[ms]").astype(float)) <= max_dt * 1000
    return [(pd.Timestamp(x), pd.Timestamp(y)) for x, y in zip(ta[ok], near[ok])]


def fit_c1_to_c3(d1, d3, iters=4, gate=90):
    src = np.array([s for s, _ in LANDMARKS_C1_C3], float); dst = np.array([t for _, t in LANDMARKS_C1_C3], float)
    f = PolyMap(1).fit(src, dst)
    d1 = d1[~d1.ir.astype(bool)]; d3 = d3[~d3.ir.astype(bool)]
    g1 = dict(tuple(d1.groupby("ts"))); g3 = dict(tuple(d3.groupby("ts")))
    pairs = _sync_pairs(d1, d3)
    pairs = pairs[::max(1, len(pairs) // 3000)]   # ~3000 synchronous frames are plenty for fitting
    hist = []
    for it in range(iters):
        S, D = [], []
        for t1, t3 in pairs:
            a, b = g1[t1], g3[t3]
            pa = f(a[["fx", "fy"]].values); pb = b[["fx", "fy"]].values
            C = np.linalg.norm(pa[:, None] - pb[None], axis=-1)
            ri, ci = linear_sum_assignment(C)
            for i, j in zip(ri, ci):
                if C[i, j] < gate:
                    S.append(a[["fx", "fy"]].values[i]); D.append(pb[j])
        S, D = np.array(S), np.array(D)
        if len(S) < 30:
            break
        deg = 1 if it == 0 else 2
        inl = np.ones(len(S), bool)
        for k in range(2):
            r = RANSACRegressor(make_pipeline(PolynomialFeatures(deg), LinearRegression()), min_samples=12, residual_threshold=45, random_state=0)
            r.fit(S, D[:, k]); inl &= r.inlier_mask_
        f = PolyMap(deg).fit(S[inl], D[inl])
        res = np.linalg.norm(f(S[inl]) - D[inl], axis=1)
        hist.append(dict(iter=it, pairs=int(len(S)), inliers=int(inl.sum()), median_px=float(np.median(res))))
        gate = max(45, gate * 0.8)
    return f, hist


def fit_c2_to_ref(d2, d3, bunk_ov=0.15, ref_ov=0.3):
    """1-D: Cam 02 box-centre x -> Cam 03 foot x, for cows at the bunk in both views.
    Cam 02 only sees part of the bunk, so counts differ between views: every
    (Cam 02 cow, Cam 03 cow) pair in synchronous frames is a candidate, and RANSAC
    finds the dominant line (true correspondences are collinear, wrong ones scatter)."""
    a = d2[(d2.bunk_ov >= bunk_ov) & ~d2.ir.astype(bool)]; b = d3[(d3.bunk_ov >= ref_ov) & ~d3.ir.astype(bool)]
    ga = dict(tuple(a.groupby("ts"))); gb = dict(tuple(b.groupby("ts")))
    X, Y = [], []
    for t2, t3 in _sync_pairs(a, b):
        xa = ((ga[t2].x1 + ga[t2].x2) / 2).values; xb = gb[t3].fx.values
        if len(xa) <= 4 and len(xb) <= 6:
            for u in xa:
                for v in xb:
                    X.append(u); Y.append(v)
    if len(X) < 40:
        return None, {"candidate_pairs": len(X)}
    X, Y = np.array(X), np.array(Y)
    r = RANSACRegressor(LinearRegression(), residual_threshold=25, max_trials=3000, random_state=0).fit(X[:, None], Y)
    info = {"candidate_pairs": int(len(X)), "inliers": int(r.inlier_mask_.sum()),
            "slope": float(r.estimator_.coef_[0]), "intercept": float(r.estimator_.intercept_)}
    if abs(info["slope"]) < 0.02 or info["inliers"] < 30:
        return None, info
    return (lambda x: r.predict(np.asarray(x, float).reshape(-1, 1))), info


def transfer_ids(dsrc, d3, mapper, mode, gate):
    """Vote Cam 03 cow ids onto source-camera tracklets via synchronous matching."""
    votes = {}
    g3 = dict(tuple(d3.groupby("ts"))); gs = dict(tuple(dsrc.groupby("ts")))
    for ts, t3 in _sync_pairs(dsrc, d3):
        a, b = gs[ts], g3[t3]
        if mode == "2d":
            pa = mapper(a[["fx", "fy"]].values); pb = b[["fx", "fy"]].values
            C = np.linalg.norm(pa[:, None] - pb[None], axis=-1)
        else:
            a = a[a.bunk_ov >= 0.15]; b = b[b.bunk_ov >= 0.3]
            if a.empty or b.empty:
                continue
            pa = mapper(((a.x1 + a.x2) / 2).values); pb = b.fx.values
            C = np.abs(pa[:, None] - pb[None])
        ri, ci = linear_sum_assignment(C)
        for i, j in zip(ri, ci):
            if C[i, j] < gate and b.cow.values[j] > 0:
                votes.setdefault(a.track.values[i], []).append(int(b.cow.values[j]))
    out = {}
    for tr, v in votes.items():
        vc = pd.Series(v).value_counts()
        out[tr] = (int(vc.index[0]), float(vc.iloc[0] / len(v)), len(v))
    return out


def link(D, out_dir=None):
    """D: dict cam -> sample dataframe with 'cow' from per-camera identity (Cam 03 anchor).
    Overwrites cow on Cam 01 / Cam 02 rows with transferred Cam 03 ids."""
    report = {}
    d3 = D.get("Cam 03")
    if d3 is None:
        return D, report
    if "Cam 01" in D and len(D["Cam 01"]):
        f, hist = fit_c1_to_c3(D["Cam 01"], d3)
        ids = transfer_ids(D["Cam 01"], d3, f, "2d", gate=70)
        d = D["Cam 01"]
        d["cow"] = d.track.map(lambda t: ids[t][0] if t in ids and ids[t][1] >= 0.5 and ids[t][2] >= 3 else 0)
        d["xcam_conf"] = d.track.map(lambda t: ids[t][1] if t in ids else 0.0)
        report["Cam 01"] = dict(fit=hist, tracklets=int(d.track.nunique()), linked=int(sum(1 for v in ids.values() if v[1] >= .5 and v[2] >= 3)))
    if "Cam 02" in D and len(D["Cam 02"]):
        # Cam 03 sees the bunk only ~300 px wide at the far end, too coarse; Cam 01 looks straight
        # along the bunk rail, so chain Cam 02 -> Cam 01 (whose ids came from Cam 03) and fall back to Cam 03.
        d = D["Cam 02"]; info = {}
        for ref_name in ("Cam 01", "Cam 03"):
            ref = D.get(ref_name)
            if ref is None or not (ref.cow > 0).any():
                continue
            f2, info = fit_c2_to_ref(d, ref[ref.cow > 0])
            info["ref"] = ref_name
            if f2 is not None:
                break
        if f2 is not None:
            ids = transfer_ids(d, ref, f2, "1d", gate=40 if ref_name == "Cam 01" else 15)
            d["cow"] = d.track.map(lambda t: ids[t][0] if t in ids and ids[t][1] >= 0.5 and ids[t][2] >= 3 else 0)
            d["xcam_conf"] = d.track.map(lambda t: ids[t][1] if t in ids else 0.0)
            info["linked"] = int(sum(1 for v in ids.values() if v[1] >= .5 and v[2] >= 3))
        else:
            d["cow"] = 0; d["xcam_conf"] = 0.0
        info["tracklets"] = int(d.track.nunique()); report["Cam 02"] = info
    if out_dir is not None:
        json.dump(report, open(out_dir / "crosscam_report.json", "w"), indent=1, default=str)
    return D, report
