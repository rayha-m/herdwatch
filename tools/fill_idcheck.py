"""Complete the ID-check set: one frame every 30 min on each camera, also when no cow was detected
(e.g. Cam 02 infrared at night) or no video exists for that slot (missing download).
    python tools/fill_idcheck.py --run full
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2, numpy as np, pandas as pd
from pipeline.config import OUT, CAMERAS
from pipeline import agent as ag

ap = argparse.ArgumentParser(); ap.add_argument("--run", default="full"); a = ap.parse_args()
d = OUT / "level1" / a.run / "id_check"
summ = pd.read_json(OUT / "level1" / a.run / "summary.json", typ="series")
t0, t1 = pd.Timestamp(summ["start"]), pd.Timestamp(summ["end"])
added = 0
for cam in CAMERAS:
    for t in pd.date_range(t0, t1, freq="30min", inclusive="left"):
        f = d / f"{cam.replace(' ', '')}_{t:%Y%m%d_%H%M}.jpg"
        if f.exists():
            continue
        img = ag.whole_frame(cam, t + pd.Timedelta(seconds=30))
        msg = "no cattle detected in Pen 6 at this moment" if img is not None else "no video for this slot (file missing from download)"
        if img is None:
            img = np.full((960, 1280, 3), 40, np.uint8)
        cv2.rectangle(img, (0, 0), (1280, 34), (0, 0, 0), -1)
        cv2.putText(img, f"{cam}  {t:%Y-%m-%d %H:%M}  (ID check): {msg}", (6, 24), 0, 0.7, (255, 255, 255), 1)
        cv2.imwrite(str(f), cv2.resize(img, (960, 720)), [cv2.IMWRITE_JPEG_QUALITY, 80]); added += 1
print("added", added, "total", len(list(d.glob("*.jpg"))))
