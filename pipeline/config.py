"""Static configuration: paths, cameras, pen zones.

All polygons are in the 1280x960 working resolution (videos are 4000x3000,
decoded at keyframes and downscaled). Zones were drawn by hand on daytime
frames (see docs/zones_*.jpg); the pen map PNG gives no waterer/resting-area
outline, so the waterer zone is the yellow tank visible on Cam 01 and Cam 03
(ASSUMPTION, see README).
"""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
VIDEO_DIR = Path(os.environ.get("BV_VIDEO_DIR", ROOT.parent / "videos"))
WORK = ROOT / "work"
OUT = ROOT / "outputs"
WEIGHTS = WORK / "weights"

W, H = 1280, 960          # working resolution
SAMPLE_PERIOD_S = 2.0     # every keyframe (GOP = 60 frames @ 30 fps)

# YOLO: COCO pretrained, all large-animal classes merged into "cattle"
# (the COCO model labels black Angus as cow / sheep / bear / horse at random)
YOLO_WEIGHTS = "yolo11s.pt"
YOLO_IMGSZ = 960
YOLO_CONF = 0.20
ANIMAL_CLASSES = [15, 16, 17, 18, 19, 20, 21, 22, 23]

N_COWS = 10

# camera folder name -> metadata.  role follows the pen map.
CAMERAS = {
    "Cam 01": dict(code="C1", role="front cam 1 (pen interior, looks back from the bunk)"),
    "Cam 02": dict(code="C2", role="front cam 2 (top-down on the feed bunk)"),
    "Cam 03": dict(code="C3", role="back cam (wide view of whole pen)"),
}

# ---- zones (x, y) in 1280x960 -------------------------------------------
# pen: region where a detection's foot point must fall to count as Pen 6
# bunk: feed bunk / head-in-trough region
# water: waterer (yellow tank, assumed)
ZONES = {
    "Cam 01": {
        "pen":   [(0, 560), (150, 330), (560, 170), (860, 165), (1280, 640), (1280, 960), (0, 960)],
        "bunk":  [(0, 800), (1280, 760), (1280, 960), (0, 960)],
        "water": [(840, 230), (990, 230), (1010, 360), (830, 360)],
    },
    "Cam 02": {
        "pen":   [(0, 0), (800, 0), (1250, 380), (1280, 960), (0, 960)],
        "bunk":  [(80, 690), (1200, 520), (1250, 660), (1100, 820), (90, 910)],
        "water": [],
    },
    "Cam 03": {
        "pen":   [(0, 500), (720, 492), (1005, 535), (1012, 565), (905, 960), (0, 960)],
        "bunk":  [(690, 495), (1005, 528), (995, 585), (690, 565)],
        "water": [(390, 500), (570, 500), (590, 600), (380, 600)],
    },
}

# How each camera is used when fusing to one label per cow-minute
# (higher = trusted more for that behaviour)
CAM_PRIORITY = {
    "feeding": {"Cam 02": 3, "Cam 01": 2, "Cam 03": 1},
    "posture": {"Cam 03": 3, "Cam 01": 2, "Cam 02": 1},
}

BEHAVIORS = [
    "lying", "standing", "walking", "feeding", "drinking",
    "ruminating_lying", "ruminating_standing", "grooming",
    "social_interaction", "other_not_visible",
]

# Daylight window (local CST) used to flag night frames; refined per frame by brightness/IR (grayscale) test
DAY_HOURS = (7, 18)

CLAUDE_MODEL = "claude-opus-5-5"
