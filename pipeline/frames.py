"""Video decoding helpers (ffmpeg, HEVC in AVI with non-standard 'GHEC' fourcc).

- iter_keyframes(): fast bulk sampling, decodes keyframes only (one every 2 s)
- grab_frame():     one full-resolution frame at an arbitrary time (agent zoom tool)
- grab_clip():      short mp4 clip for evidence / agent "look in time" tool
"""
import re, subprocess, threading
import numpy as np
from .config import W, H

FF = ["ffmpeg", "-hide_banner", "-nostdin"]


def real_path(path):
    """'zip:<entry>' -> extract into the LRU cache and return a file path (random access)."""
    path = str(path)
    if path.startswith("zip:"):
        from . import zipstore
        return zipstore.materialize(path[4:])
    return path


def iter_keyframes(path, w=W, h=H):
    """Yield (t_rel_seconds, bgr ndarray[h,w,3]) for every keyframe.
    'zip:' paths are streamed from the ZIP part into ffmpeg's stdin (no extraction)."""
    path = str(path)
    src = ["-f", "avi", "-i", "pipe:0"] if path.startswith("zip:") else ["-i", path]
    cmd = FF + ["-loglevel", "info", "-hwaccel", "videotoolbox", "-c:v", "hevc",
                "-skip_frame", "nokey"] + src + [
                "-vf", f"scale={w}:{h},showinfo", "-fps_mode", "passthrough",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.PIPE if path.startswith("zip:") else None, bufsize=10**8)
    if path.startswith("zip:"):
        from . import zipstore

        def feed():
            try:
                for b in zipstore.stream(path[4:]):
                    p.stdin.write(b)
            except (BrokenPipeError, ValueError):
                pass
            finally:
                try:
                    p.stdin.close()
                except Exception:
                    pass
        threading.Thread(target=feed, daemon=True).start()
    times = []
    pat = re.compile(rb"pts_time:([0-9.]+)")

    def read_err():
        for line in p.stderr:
            m = pat.search(line)
            if m:
                times.append(float(m.group(1)))
    th = threading.Thread(target=read_err, daemon=True)
    th.start()
    n = w * h * 3
    i = 0
    while True:
        buf = p.stdout.read(n)
        if len(buf) < n:
            break
        # showinfo line for frame i may lag slightly behind stdout; wait for it
        for _ in range(200):
            if len(times) > i or not th.is_alive():
                break
            threading.Event().wait(0.005)
        t = times[i] if i < len(times) else i * 2.0
        yield t, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        i += 1
    p.wait()


def grab_frame(path, t, scale_w=None, crop=None):
    """Full-res (4000x3000) frame at t seconds; optional crop=(x,y,w,h) in *full-res* px."""
    vf = []
    if crop:
        x, y, cw, ch = [int(v) for v in crop]
        vf.append(f"crop={cw}:{ch}:{x}:{y}")
    if scale_w:
        vf.append(f"scale={scale_w}:-2")
    cmd = FF + ["-loglevel", "fatal", "-ss", f"{max(t, 0):.2f}", "-c:v", "hevc", "-i", real_path(path),
                "-frames:v", "1"] + (["-vf", ",".join(vf)] if vf else []) + ["-f", "image2pipe", "-vcodec", "png", "-"]
    r = subprocess.run(cmd, capture_output=True)
    import cv2
    a = np.frombuffer(r.stdout, np.uint8)
    return cv2.imdecode(a, cv2.IMREAD_COLOR) if a.size else None


def grab_clip(path, t0, dur, out, scale_w=960, fps=15, crf=28):
    cmd = FF + ["-loglevel", "fatal", "-y", "-ss", f"{max(t0, 0):.2f}", "-c:v", "hevc", "-i", real_path(path),
                "-t", f"{dur:.2f}", "-an", "-vf", f"scale={scale_w}:-2,fps={fps}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    return out
