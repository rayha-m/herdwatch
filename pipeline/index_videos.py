"""Build work/video_index.csv: one row per AVI with camera, day and absolute start time.

Filenames are EventYYYYMMDDHHMMSS<cam-suffix>.Avi; duration comes from ffprobe.
    python -m pipeline.index_videos
"""
import re, subprocess, json
from datetime import datetime, timedelta
import pandas as pd
from .config import VIDEO_DIR, WORK, CAMERAS

PAT = re.compile(r"Event(\d{14})\d{3}\.avi$", re.I)


def probe_duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "json", str(p)], capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return float("nan")


def avi_duration(first_bytes):
    """Duration from the AVI main header (avih): dwMicroSecPerFrame * dwTotalFrames."""
    import struct
    i = first_bytes.find(b"avih")
    if i < 0:
        return float("nan")
    usec, = struct.unpack("<I", first_bytes[i + 8:i + 12])
    total, = struct.unpack("<I", first_bytes[i + 24:i + 28])
    return usec * total / 1e6


def build():
    """Index videos from the Drive ZIP parts (preferred, see zipstore) or from VIDEO_DIR."""
    from . import zipstore
    rows = []
    if zipstore.INDEX.exists():
        for name, e in zipstore.entries().items():
            parts = name.split("/")
            day, cam, fname = parts[-3], parts[-2], parts[-1]
            m = PAT.search(fname)
            if not m or cam not in CAMERAS:
                continue
            head = next(zipstore.stream(name))[:4096]
            rows.append(dict(day=day, cam=cam, path="zip:" + name, file=fname,
                             start=datetime.strptime(m.group(1), "%Y%m%d%H%M%S"),
                             size_mb=e["comp"] / 2**20, duration_s=avi_duration(head)))
    else:
        for day_dir in sorted(VIDEO_DIR.glob("Day *")):
            for cam in CAMERAS:
                for p in sorted((day_dir / cam).glob("*.[Aa]vi")):
                    m = PAT.search(p.name)
                    if m:
                        rows.append(dict(day=day_dir.name, cam=cam, path=str(p), file=p.name,
                                         start=datetime.strptime(m.group(1), "%Y%m%d%H%M%S"),
                                         size_mb=p.stat().st_size / 2**20, duration_s=probe_duration(p)))
    df = pd.DataFrame(rows)
    df["end"] = df.start + pd.to_timedelta(df.duration_s, unit="s")
    df = df.sort_values(["cam", "start"]).reset_index(drop=True)
    WORK.mkdir(parents=True, exist_ok=True)
    df.to_csv(WORK / "video_index.csv", index=False)
    return df


if __name__ == "__main__":
    df = build()
    print(df.groupby(["day", "cam"]).agg(files=("file", "size"), hours=("duration_s", lambda s: s.sum() / 3600),
                                          first=("start", "min"), last=("end", "max")))
