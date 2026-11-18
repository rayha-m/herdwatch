"""Stage 2: tracklets -> 10 persistent cow identities.

Black Angus look alike, so identity uses every cue we have:
  * colour signature (90th-percentile fraction of yellow / red-pink / blue tag
    pixels and white face/neck markings over the tracklet), the strongest cue;
  * ResNet appearance embedding (mean over the tracklet);
  * motion plausibility (a cow cannot jump across the pen between tracklets);
  * exclusivity: two tracklets alive at the same time are never the same cow.

Algorithm: process tracklets in start order, assign each to one of N_COWS identity
slots that is free at that time by Hungarian matching on the combined cost.
Slots are created until there are 10, then only re-used. Prototypes are updated
with an EMA weighted by tracklet length. Assignments whose margin to the
second-best slot is small are flagged for the agent (identity hard cases).
"""
import numpy as np, pandas as pd
from scipy.optimize import linear_sum_assignment
from .config import N_COWS

COLS = ["yellow", "red", "green", "blue", "white"]


def tracklets(d, E):
    d = d.copy(); d["_i"] = np.arange(len(d))
    rows = []
    for tid, g in d.groupby("track", sort=False):
        g = g.sort_values("ts")
        e = E[g._i.values].astype(np.float32).mean(0); e /= np.linalg.norm(e) + 1e-6
        col = {c: float(g[c].quantile(0.9)) if c in g else 0.0 for c in COLS}
        rows.append(dict(track=tid, cam=g.cam.iloc[0], t0=g.ts.iloc[0], t1=g.ts.iloc[-1], n=len(g),
                         x0=g.fx.iloc[0], y0=g.fy.iloc[0], x1=g.fx.iloc[-1], y1=g.fy.iloc[-1],
                         wmed=float(g.w.median()), ir=float(g.ir.mean()), emb=e, **col, conf=float(g.conf.mean()),
                         p_stand=float(g.p_stand.mean()) if "p_stand" in g else np.nan))
    return pd.DataFrame(rows).sort_values("t0").reset_index(drop=True)


def colour_vec(r):
    # log-scale so a small but consistent tag signal counts
    return np.log1p(np.array([getattr(r, c) for c in COLS], dtype=float) * 400.0)


class Slot:
    def __init__(self, k, r):
        self.k = k; self.emb = r.emb.copy(); self.col = colour_vec(r); self.mass = r.n
        self.busy_until = r.t1; self.x, self.y, self.t = r.x1, r.y1, r.t1; self.ir_seen = r.ir > 0.5

    def update(self, r):
        a = min(r.n / (self.mass + r.n), 0.5)
        if r.ir < 0.5:          # don't let night (grayscale) tracklets overwrite colour prototype
            self.col = (1 - a) * self.col + a * colour_vec(r)
            self.emb = (1 - a) * self.emb + a * r.emb; self.emb /= np.linalg.norm(self.emb) + 1e-6
        self.mass += r.n; self.busy_until = max(self.busy_until, r.t1); self.x, self.y, self.t = r.x1, r.y1, r.t1


def cost(slot, r):
    c_emb = 1 - float(slot.emb @ r.emb)                           # ~0.05 (same) .. 0.4
    c_col = float(np.abs(slot.col - colour_vec(r)).sum()) / 5.0   # 0 .. ~1.5
    if r.ir > 0.5:
        c_col *= 0.3                                               # colour unreliable in IR
    dt = max((r.t0 - slot.t).total_seconds(), 2.0)
    dist = np.hypot(r.x0 - slot.x, r.y0 - slot.y) / max(r.wmed, 20)  # in body widths
    speed = dist / dt                                              # body widths per second
    c_mot = min(dist, 12) * (0.25 if dt < 30 else 0.08 if dt < 300 else 0.02) + (1.0 if speed > 1.0 else 0)
    return 2.0 * c_emb + 1.0 * c_col + c_mot


def assign(tr, n_ids=N_COWS, margin_flag=0.25, gap_s=0.5, core_n=4, core_conf=0.35):
    """Core tracklets (long, confident) claim identity slots; fragments are attached afterwards."""
    full = tr
    core_mask = (tr.n >= core_n) & (tr.conf >= core_conf)
    tr = tr[core_mask].reset_index(drop=True)
    slots, lab, conf, margin = [], {}, {}, {}
    groups = tr.groupby(tr.t0.dt.floor("2s"), sort=True)
    for _, g in groups:
        t0 = g.t0.min()          # real start (not the 2-s floor: that kept slots busy at file boundaries)
        free = [s for s in slots if s.busy_until < t0 - pd.Timedelta(seconds=gap_s)]
        rows = list(g.itertuples())
        if free:
            C = np.array([[cost(s, r) for s in free] for r in rows])
            # allow 'new slot' while < n_ids exist
            n_new = max(0, n_ids - len(slots))
            if n_new:
                C = np.hstack([C, np.full((len(rows), n_new), 1.6)])
            ri, ci = linear_sum_assignment(C)
            for i, j in zip(ri, ci):
                r = rows[i]
                if j < len(free):
                    s = free[j]; s.update(r)
                    srt = np.sort(C[i]); m = float(srt[1] - srt[0]) if len(srt) > 1 else 9.0
                    lab[r.track] = s.k; margin[r.track] = m; conf[r.track] = float(np.exp(-C[i, j]))
                else:
                    s = Slot(len(slots) + 1, r); slots.append(s)
                    lab[r.track] = s.k; margin[r.track] = 9.0; conf[r.track] = 0.5
            unassigned = [rows[i] for i in range(len(rows)) if i not in set(ri)]
        else:
            unassigned = rows
        for r in unassigned:
            if len(slots) < n_ids:
                s = Slot(len(slots) + 1, r); slots.append(s)
                lab[r.track] = s.k; margin[r.track] = 9.0; conf[r.track] = 0.5
            else:   # more simultaneous tracklets than cows -> duplicate / false positive
                lab[r.track] = 0; margin[r.track] = 0.0; conf[r.track] = 0.0
    # fragments: attach to the identity of the nearest core tracklet in space and time, if that
    # identity is not visibly elsewhere at the same moment; otherwise leave unassigned (0)
    core = tr.assign(cow=tr.track.map(lab)).sort_values("t1").reset_index(drop=True)
    t1s = core.t1.values
    for r in full[~core_mask].itertuples():
        best, bd = 0, 9e9
        # only core tracklets that end within 30 s before r starts, or start within 30 s after r ends
        lo = np.searchsorted(t1s, np.datetime64(r.t0 - pd.Timedelta(seconds=30)))
        cand = core.iloc[lo:]
        cand = cand[cand.t0 <= r.t1 + pd.Timedelta(seconds=30)]
        for c in cand.itertuples():
            if not c.cow:
                continue
            overlap = (c.t0 <= r.t1) and (r.t0 <= c.t1)
            if overlap:
                continue          # that cow is visible elsewhere at the same time
            dt = min(abs((r.t0 - c.t1).total_seconds()), abs((c.t0 - r.t1).total_seconds()))
            if dt > 30:
                continue
            ex, ey = (c.x1, c.y1) if c.t1 <= r.t0 else (c.x0, c.y0)
            dist = np.hypot(r.x0 - ex, r.y0 - ey) / max(r.wmed, 20)
            if dist < 1.5 and dist + dt / 30 < bd:
                best, bd = c.cow, dist + dt / 30
        lab[r.track] = best; conf[r.track] = 0.3 if best else 0.0; margin[r.track] = 0.0
    tr = full.copy()
    tr["cow"] = tr.track.map(lab).fillna(0).astype(int); tr["id_conf"] = tr.track.map(conf).fillna(0)
    tr["id_margin"] = tr.track.map(margin).fillna(0)
    tr["id_hard"] = (tr.id_margin < margin_flag) & (tr.n >= 5) & core_mask
    return tr, slots
