"""Render the hand-drawn zones (pen / bunk / waterer) on one daytime frame per camera -> docs/zones_<cam>.jpg
    python tools/draw_zones.py --at "2026-11-08 12:30"
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2, numpy as np, pandas as pd
from pipeline.config import ZONES, ROOT, W, H
from pipeline.agent import locate
from pipeline.frames import grab_frame

COL = {"pen": (0, 255, 0), "bunk": (0, 165, 255), "water": (255, 160, 0)}

ap = argparse.ArgumentParser(); ap.add_argument("--at", default="2026-11-08 12:30"); a = ap.parse_args()
(ROOT / "docs").mkdir(exist_ok=True)
for cam, zs in ZONES.items():
    path, t = locate(cam, a.at)
    if path is None:
        print("no video", cam); continue
    img = cv2.resize(grab_frame(path, t, scale_w=W), (W, H))
    over = img.copy()
    for z, pts in zs.items():
        if pts:
            p = np.array(pts, np.int32)
            cv2.fillPoly(over, [p], COL[z]) if z != "pen" else None
            cv2.polylines(img, [p], True, COL[z], 3)
            cv2.putText(img, z, tuple(p.min(0) + [6, 28]), 0, 0.9, COL[z], 2)
    img = cv2.addWeighted(over, 0.25, img, 0.75, 0)
    cv2.putText(img, f"{cam}  {a.at}", (10, H - 16), 0, 0.8, (255, 255, 255), 2)
    cv2.imwrite(str(ROOT / "docs" / f"zones_{cam.replace(' ', '')}.jpg"), cv2.resize(img, (960, 720)))
    print("wrote", cam)
