"""Stage 1 (bulk, no LLM): YOLO detection + per-camera tracking + per-detection features.

For each video: decode keyframes (0.5 fps) -> YOLO11 (COCO, animal classes merged)
-> keep detections whose foot point is inside the Pen 6 polygon -> IoU/centre
tracker -> per-detection features (zones, posture geometry, ear-tag colour,
appearance embedding). Output: work/det/<cam>/<file>.parquet + .npy embeddings.

    python -m pipeline.detect_track --cam "Cam 03" --start "2026-11-07 12:00" --hours 1
"""
import argparse, time
from datetime import datetime, timedelta
import numpy as np, pandas as pd, cv2, torch
from scipy.optimize import linear_sum_assignment
from ultralytics import YOLO
from .config import (WORK, WEIGHTS, YOLO_WEIGHTS, YOLO_IMGSZ, YOLO_CONF, ANIMAL_CLASSES, ZONES, W, H)
from .frames import iter_keyframes

DEV = "mps" if torch.backends.mps.is_available() else "cpu"


# ----------------------------------------------------------------- geometry
def poly_mask(poly):
    m = np.zeros((H, W), np.uint8)
    if poly:
        cv2.fillPoly(m, [np.array(poly, np.int32)], 1)
    return m


def iou(a, b):
    x1 = np.maximum(a[:, None, 0], b[None, :, 0]); y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2]); y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1]); bb = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / (aa[:, None] + bb[None, :] - inter + 1e-6)


class Tracker:
    """Greedy-optimal IoU + normalised-centre-distance tracker for 0.5 fps cattle.
    Cows move little in 2 s, so IoU works; walking cows fall back to centre distance."""

    def __init__(self, max_age=15, prefix=""):
        self.tracks = {}   # id -> dict(box, last_t, age)
        self.next = 1; self.max_age = max_age; self.prefix = prefix

    def update(self, boxes, t):
        ids = [None] * len(boxes)
        live = [k for k, v in self.tracks.items() if v["miss"] <= self.max_age]
        if live and len(boxes):
            tb = np.array([self.tracks[k]["box"] for k in live])
            I = iou(boxes, tb)
            c1 = (boxes[:, :2] + boxes[:, 2:]) / 2; c2 = (tb[:, :2] + tb[:, 2:]) / 2
            diag = np.sqrt(((tb[:, 2:] - tb[:, :2]) ** 2).sum(1))
            D = np.sqrt(((c1[:, None] - c2[None]) ** 2).sum(-1)) / (diag[None] + 1e-6)
            cost = 1 - I + 0.5 * np.clip(D, 0, 2)
            r, c = linear_sum_assignment(cost)
            for i, j in zip(r, c):
                if I[i, j] > 0.2 or D[i, j] < 0.6:
                    ids[i] = live[j]
        for k in self.tracks:
            self.tracks[k]["miss"] += 1
        for i, b in enumerate(boxes):
            if ids[i] is None:
                ids[i] = f"{self.prefix}{self.next}"; self.next += 1
            self.tracks[ids[i]] = dict(box=b, miss=0)
        return ids


# ----------------------------------------------------------------- features
def tag_colour_fracs(crop):
    """Fractions of saturated tag colours + white (face blaze / white patches) in the crop.
    These are the strongest identity cues for black Angus (body colour carries almost none)."""
    out = dict(yellow=0.0, red=0.0, green=0.0, blue=0.0, white=0.0)
    if crop.size == 0:
        return out
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    out["yellow"] = float(((h >= 20) & (h <= 35) & (s > 110) & (v > 110)).mean())
    out["red"] = float((((h <= 8) | (h >= 155)) & (s > 110) & (v > 90)).mean())
    out["green"] = float(((h >= 40) & (h <= 85) & (s > 80) & (v > 70)).mean())   # green tags
    out["blue"] = float(((h >= 86) & (h <= 125) & (s > 70) & (v > 90)).mean())  # light-blue tags
    out["white"] = float(((s < 45) & (v > 185)).mean())
    return out


class Embedder:
    """ResNet-18 (ImageNet) global-pooled features of each crop for appearance re-ID."""

    def __init__(self):
        import torchvision
        m = torchvision.models.resnet18(weights=torchvision.models.ResNet18_Weights.IMAGENET1K_V1)
        m.fc = torch.nn.Identity(); self.m = m.eval().to(DEV)
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=DEV).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=DEV).view(1, 3, 1, 1)

    @torch.no_grad()
    def __call__(self, crops):
        if not crops:
            return np.zeros((0, 512), np.float16)
        x = np.stack([cv2.resize(c, (128, 128))[:, :, ::-1] for c in crops]).astype(np.float32) / 255
        x = torch.from_numpy(x).permute(0, 3, 1, 2).to(DEV)
        f = self.m((x - self.mean) / self.std)
        f = torch.nn.functional.normalize(f, dim=1)
        return f.cpu().numpy().astype(np.float16)


def frame_is_ir(img):
    """Night IR footage is grayscale: very low mean saturation."""
    small = cv2.resize(img, (160, 120))
    return float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 1].mean()) < 12


# ----------------------------------------------------------------- main
def process_video(row, model, emb, out_dir, batch=8):
    cam = row.cam
    masks = {k: poly_mask(v) for k, v in ZONES[cam].items()}
    trk = Tracker(prefix=f"{row.file[:-4]}_")
    recs, embs = [], []
    buf = []

    def flush():
        if not buf:
            return
        res = model.predict([f for _, f in buf], imgsz=YOLO_IMGSZ, conf=YOLO_CONF, classes=ANIMAL_CLASSES,
                            device=DEV, verbose=False)
        for (t, img), r in zip(buf, res):
            b = r.boxes.xyxy.cpu().numpy() if len(r.boxes) else np.zeros((0, 4))
            cf = r.boxes.conf.cpu().numpy() if len(r.boxes) else np.zeros(0)
            # class-agnostic NMS across merged animal classes
            if len(b):
                xywh = [[float(x[0]), float(x[1]), float(x[2] - x[0]), float(x[3] - x[1])] for x in b]
                keep = cv2.dnn.NMSBoxes(xywh, cf.tolist(), YOLO_CONF, 0.6)
                keep = np.array(keep).reshape(-1)
                b, cf = b[keep], cf[keep]
            # foot point inside pen?
            fx = np.clip(((b[:, 0] + b[:, 2]) / 2).astype(int), 0, W - 1); fy = np.clip(b[:, 3].astype(int) - 1, 0, H - 1)
            inpen = masks["pen"][fy, fx] > 0 if len(b) else np.zeros(0, bool)
            b, cf = b[inpen], cf[inpen]
            ids = trk.update(b, t)
            ir = frame_is_ir(img)
            bright = float(img.mean())
            crops = []
            for (x1, y1, x2, y2), c, tid in zip(b, cf, ids):
                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                crop = img[max(y1, 0):y2, max(x1, 0):x2]
                crops.append(crop)
                col = tag_colour_fracs(crop)
                area = max((x2 - x1) * (y2 - y1), 1)
                def ov(k):
                    m = masks.get(k)
                    return float((m[y1:y2, x1:x2] > 0).sum() / area) if m is not None and m.any() else 0.0
                recs.append(dict(cam=cam, day=row.day, file=row.file, t_rel=t,
                                 ts=row.start + timedelta(seconds=t), track=tid, conf=float(c),
                                 x1=x1, y1=y1, x2=x2, y2=y2, w=x2 - x1, h=y2 - y1, aspect=(x2 - x1) / max(y2 - y1, 1),
                                 fx=(x1 + x2) / 2, fy=y2, bunk_ov=ov("bunk"), water_ov=ov("water"),
                                 **col, ir=ir, bright=bright))
            embs.append(emb(crops))
        buf.clear()

    for t, img in iter_keyframes(row.path):
        buf.append((t, img.copy()))
        if len(buf) >= batch:
            flush()
    flush()
    df = pd.DataFrame(recs)
    E = np.concatenate(embs) if embs else np.zeros((0, 512), np.float16)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / row.file[:-4]
    df.to_parquet(f"{stem}.parquet")
    np.save(f"{stem}.npy", E)
    return len(df)


def select(index, cam=None, start=None, hours=None, days=None):
    df = index.copy()
    if cam:
        df = df[df.cam == cam]
    if days:
        df = df[df.day.isin(days)]
    if start:
        s = pd.Timestamp(start); e = s + pd.Timedelta(hours=hours or 1)
        df = df[(df.end > s) & (df.start < e)]
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam"); ap.add_argument("--start"); ap.add_argument("--hours", type=float)
    ap.add_argument("--all", action="store_true"); ap.add_argument("--force", action="store_true")
    ap.add_argument("--reverse", action="store_true", help="process newest files first (lets two workers meet in the middle)")
    a = ap.parse_args()
    index = pd.read_csv(WORK / "video_index.csv", parse_dates=["start", "end"])
    rows = index if a.all else select(index, a.cam, a.start, a.hours)
    if a.reverse:
        rows = rows.iloc[::-1]
    model = YOLO(str(WEIGHTS / YOLO_WEIGHTS)); emb = Embedder()
    t0 = time.time(); n = 0
    for i, row in enumerate(rows.itertuples()):
        out_dir = WORK / "det" / row.cam.replace(" ", "")
        if (out_dir / (row.file[:-4] + ".parquet")).exists() and not a.force:
            continue
        k = process_video(row, model, emb, out_dir); n += 1
        el = time.time() - t0
        print(f"[{i+1}/{len(rows)}] {row.cam} {row.file} {row.duration_s/60:.1f}min dets={k} elapsed={el/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
