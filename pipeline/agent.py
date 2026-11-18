"""Stage 3: Claude vision agent for HARD cases only.

The agent gets a question about one cow-track at one moment, and tools to
  * look            - full-resolution crop of the cow (zoom)        [frames.grab_frame]
  * look_sequence   - the same crop at several offsets in time      (look back / forth)
  * look_other_camera - what the other cameras show at that moment (cross-view check)
  * chew_check      - 4 s @30 fps jaw-region motion periodicity     (rumination evidence)
  * submit          - structured answer + confidence + what it checked
The loop then runs a programmatic SELF-CHECK on the submitted answer against
geometry (bunk/waterer zones, posture box shape). If they conflict, the agent is
told why and must look again before re-submitting (max one extra round).

Every call goes through llm.create (hard budget + ledger). Every case is saved
as a reasoning trace in work/agent_traces/<case_id>.json (+ images).
"""
import base64, json, time
from datetime import timedelta
from pathlib import Path
import numpy as np, pandas as pd, cv2
from .config import WORK, ZONES, BEHAVIORS, W, H, CAMERAS
from .frames import grab_frame, grab_clip, real_path
from . import llm

FULL_W, FULL_H = 4000, 3000
SX, SY = FULL_W / W, FULL_H / H
TRACE_DIR = WORK / "agent_traces"
MAX_ROUNDS = 7

_index = None


def index():
    global _index
    if _index is None:
        _index = pd.read_csv(WORK / "video_index.csv", parse_dates=["start", "end"])
    return _index


LIVE_SOURCES = []   # (cam, start, end, path) for clips not in the event index (Level 3)


def register_live(cam, start, duration_s, path):
    LIVE_SOURCES.append((cam, pd.Timestamp(start), pd.Timestamp(start) + pd.Timedelta(seconds=duration_s), str(path)))


def locate(cam, ts):
    ts = pd.Timestamp(ts)
    for c, s, e, p in LIVE_SOURCES:
        if c == cam and s <= ts < e:
            return p, (ts - s).total_seconds()
    df = index()
    r = df[(df.cam == cam) & (df.start <= ts) & (df.end > ts)]
    if r.empty:
        return None, None
    r = r.iloc[0]
    return r.path, (ts - r.start).total_seconds()


def enc(img, maxw=1024, q=80):
    if img is None:
        return None
    h, w = img.shape[:2]
    if w > maxw:
        img = cv2.resize(img, (maxw, int(h * maxw / w)))
    ok, b = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return base64.standard_b64encode(b.tobytes()).decode()


def img_block(b64):
    if not b64:      # frame could not be decoded (gap in the video, or crop outside the frame)
        return {"type": "text", "text": "(image unavailable for this time/camera)"}
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


def crop_full(cam, ts, box, pad=0.35, maxw=900):
    """box in 1280x960 working coords -> padded full-res crop."""
    path, t = locate(cam, ts)
    if path is None:
        return None
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    x1, y1 = max(0, x1 - pad * bw), max(0, y1 - pad * bh)
    x2, y2 = min(W, x2 + pad * bw), min(H, y2 + pad * bh)
    img = grab_frame(path, t, crop=(x1 * SX, y1 * SY, (x2 - x1) * SX, (y2 - y1) * SY))
    return img


def whole_frame(cam, ts, boxes=None, maxw=1024):
    path, t = locate(cam, ts)
    if path is None:
        return None
    img = grab_frame(path, t, scale_w=W)
    if img is None:
        return None
    img = cv2.resize(img, (W, H))
    for lab, (x1, y1, x2, y2), col in (boxes or []):
        cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), col, 3)
        cv2.putText(img, str(lab), (int(x1) + 4, int(y1) + 26), 0, 0.9, col, 2)
    for zname, col in (("bunk", (0, 200, 255)), ("water", (255, 200, 0))):
        pts = ZONES[cam].get(zname)
        if pts:
            cv2.polylines(img, [np.array(pts, np.int32)], True, col, 1)
    return img


def chew_signal(cam, ts, box):
    """Rumination evidence: 8 s @ 30 fps of the cow box; dominant frequency of
    the motion energy. Cattle chew cud at ~0.8-1.3 Hz."""
    path, t = locate(cam, ts)
    if path is None:
        return {"error": "no video"}
    tmp = WORK / "tmp_chew.mp4"
    x1, y1, x2, y2 = box
    try:
        import subprocess
        cw, ch = (x2 - x1) * SX, (y2 - y1) * SY
        subprocess.run(["ffmpeg", "-loglevel", "fatal", "-y", "-ss", f"{t:.2f}", "-c:v", "hevc", "-i", real_path(path), "-t", "8",
                        "-vf", f"crop={int(cw)}:{int(ch)}:{int(x1*SX)}:{int(y1*SY)},scale=160:-2,format=gray",
                        "-c:v", "libx264", "-crf", "18", str(tmp)], check=True)
    except Exception as e:
        return {"error": str(e)}
    cap = cv2.VideoCapture(str(tmp)); fr = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        fr.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32))
    if len(fr) < 60:
        return {"error": "too few frames"}
    d = np.array([np.abs(fr[i] - fr[i - 1]).mean() for i in range(1, len(fr))])
    d = d - d.mean()
    spec = np.abs(np.fft.rfft(d)); freqs = np.fft.rfftfreq(len(d), 1 / 30)
    band = (freqs > 0.3) & (freqs < 4)
    f0 = float(freqs[band][spec[band].argmax()])
    ratio = float(spec[band].max() / (np.median(spec[band]) + 1e-6))
    return {"dominant_hz": round(f0, 2), "peak_to_median": round(ratio, 1),
            "motion_energy": round(float(np.abs(d).mean()), 3),
            "interpretation_hint": "rhythmic 0.7-1.4 Hz with peak_to_median > 4 suggests chewing (rumination)"}


TOOLS = [
    {"name": "look", "description": "Zoomed full-resolution crop around the cow's box at a time offset (seconds, relative to the case time). Use to check head position, ears, mouth, nose, legs, tag colour.",
     "input_schema": {"type": "object", "properties": {"offset_s": {"type": "number"}, "pad": {"type": "number", "description": "extra margin around the box as a fraction of its size, 0.1-2"}},
                      "required": ["offset_s", "pad"], "additionalProperties": False}, "strict": True},
    {"name": "look_sequence", "description": "Whole-camera frames (1024 px, cow boxed, bunk zone orange, waterer zone blue) at several time offsets in seconds, to see movement or what happens before/after. Max 4 offsets.",
     "input_schema": {"type": "object", "properties": {"offsets_s": {"type": "array", "items": {"type": "number"}}},
                      "required": ["offsets_s"], "additionalProperties": False}, "strict": True},
    {"name": "look_other_camera", "description": "Whole frame from another camera at the same moment (cameras: 'Cam 01' front/pen interior, 'Cam 02' top-down on feed bunk, 'Cam 03' back/wide).",
     "input_schema": {"type": "object", "properties": {"camera": {"type": "string", "enum": list(CAMERAS)}, "offset_s": {"type": "number"}},
                      "required": ["camera", "offset_s"], "additionalProperties": False}, "strict": True},
    {"name": "chew_check", "description": "Measures rhythmic jaw/head motion over 8 s at 30 fps in the cow's box (rumination evidence). Returns dominant frequency and peak strength.",
     "input_schema": {"type": "object", "properties": {"offset_s": {"type": "number"}}, "required": ["offset_s"], "additionalProperties": False}, "strict": True},
    {"name": "submit", "description": "Final answer. Call exactly once when confident, or when evidence is exhausted (then use low confidence / other_not_visible).",
     "input_schema": {"type": "object", "properties": {
         "behavior": {"type": "string", "enum": BEHAVIORS},
         "posture": {"type": "string", "enum": ["lying", "standing", "unknown"]},
         "same_cow_as_candidate": {"type": "string", "description": "for identity questions: the candidate label you matched, or 'none'/'n/a'"},
         "clinical_signs": {"type": "array", "items": {"type": "string", "enum": ["drooped_ears", "head_low", "nasal_discharge", "ocular_discharge", "laboured_breathing", "isolated", "gaunt_flank", "none_seen", "not_assessable"]}},
         "confidence": {"type": "number"},
         "evidence": {"type": "string", "description": "what you looked at and what it showed"},
         "self_check": {"type": "string", "description": "how you verified the answer (second view, time offset, chew test) and what would make it wrong"}},
         "required": ["behavior", "posture", "same_cow_as_candidate", "clinical_signs", "confidence", "evidence", "self_check"], "additionalProperties": False}, "strict": True},
]

SYSTEM = """You are a careful livestock-behaviour observer working inside a camera pipeline for Pen 6 (10 black Angus cows, three fisheye cameras, Texas). A fast YOLO tracker has already labelled most frames; you are only called for the hard cases it could not resolve.

Plan before looking: decide which evidence would settle the question, then use the tools to get it. Prefer a zoomed look first, then check over time (look_sequence) or from another camera, and use chew_check before calling anything rumination. Night footage is infrared grayscale and often unreadable; say so instead of guessing.

Behaviour definitions (label what the cow is doing for most of the minute around the case time):
- feeding: head in or over the feed bunk with the muzzle at the feed.
- drinking: muzzle at the waterer.
- ruminating_lying / ruminating_standing: rhythmic cud chewing (chew_check rhythmic ~0.7-1.4 Hz) with no feed in front of her.
- lying / standing: posture with none of the above activities.
- walking: moving across the pen (position changes over the sequence).
- grooming: licking or scratching herself or a fixture.
- social_interaction: head-to-head, butting, licking another cow, mounting.
- other_not_visible: occluded, out of view, or too dark to decide.

Also note any visible clinical signs of respiratory disease (drooped ears, head carried low, nasal or ocular discharge, laboured breathing, gaunt flank, standing apart from the herd). Only list signs you actually saw; use not_assessable when resolution or light does not allow it.

Before you submit, check your answer with at least one independent view (another time offset, another camera, or chew_check), and state in self_check what you verified. Report confidence honestly between 0 and 1."""


def _geometry_check(case, ans):
    """Programmatic self-check: does the answer contradict the geometry we know?"""
    issues = []
    b = ans.get("behavior")
    if b == "feeding" and case.get("bunk_ov", 0) < 0.05 and case["cam"] != "Cam 01":
        issues.append(f"you said feeding but the cow's box overlaps the bunk zone by only {case.get('bunk_ov',0):.0%} on {case['cam']}")
    if b == "drinking" and case.get("water_ov", 0) < 0.02:
        issues.append(f"you said drinking but the box is not near the waterer zone (overlap {case.get('water_ov',0):.0%})")
    if b in ("lying", "ruminating_lying") and case.get("posture_prob_standing", 0.5) > 0.9:
        issues.append("the posture model is >90% sure this cow is standing")
    if b in ("standing", "ruminating_standing", "feeding", "walking") and case.get("posture_prob_standing", 0.5) < 0.1:
        issues.append("the posture model is >90% sure this cow is lying")
    if b.startswith("ruminating") and "chew" not in ans.get("self_check", "").lower():
        issues.append("rumination was claimed without a chew_check")
    return issues


def run_case(case, save=True):
    """case: dict(case_id, kind, question, cam, ts, box[x1,y1,x2,y2], bunk_ov, water_ov,
    posture_prob_standing, context(str), candidates(list of (label, cam, ts, box)) optional)"""
    cam, ts, box = case["cam"], pd.Timestamp(case["ts"]), case["box"]
    trace = {"case": {k: (str(v) if k == "ts" else v) for k, v in case.items()}, "steps": [], "started": time.strftime("%H:%M:%S")}
    imgdir = TRACE_DIR / case["case_id"]; imgdir.mkdir(parents=True, exist_ok=True)
    nimg = [0]

    def keep(img, tag):
        nimg[0] += 1
        p = imgdir / f"{nimg[0]:02d}_{tag}.jpg"
        if img is not None:
            cv2.imwrite(str(p), img if img.shape[1] <= 1024 else cv2.resize(img, (1024, int(img.shape[0] * 1024 / img.shape[1]))))
        return p.name

    first = whole_frame(cam, ts, [("?", box, (0, 255, 0))])
    zoom = crop_full(cam, ts, box)
    content = [{"type": "text", "text": f"CASE {case['case_id']} ({case['kind']}). Camera {cam} ({CAMERAS[cam]['role']}), time {ts}.\n"
                                        f"Tracker context: {case.get('context','')}\nQuestion: {case['question']}"},
               {"type": "text", "text": "Whole frame (cow boxed in green):"}, img_block(enc(first)),
               {"type": "text", "text": "Zoomed full-resolution crop:"}, img_block(enc(zoom, 900))]
    trace["steps"].append({"role": "pipeline", "text": content[0]["text"], "images": [keep(first, "frame"), keep(zoom, "zoom")]})
    for lab, ccam, cts, cbox in case.get("candidates", []):
        cimg = crop_full(ccam, cts, cbox, pad=0.2)
        content += [{"type": "text", "text": f"Candidate {lab} (reference crop from {ccam} {cts}):"}, img_block(enc(cimg, 500))]
        trace["steps"].append({"role": "pipeline", "text": f"candidate {lab}", "images": [keep(cimg, f"cand_{lab}")]})
    messages = [{"role": "user", "content": content}]
    answer, rechecked = None, False
    for rnd in range(MAX_ROUNDS):
        try:
            msg = llm.create(f"agent:{case['kind']}", max_tokens=8000, system=SYSTEM, tools=TOOLS,
                             output_config={"effort": "medium"}, messages=messages)
        except llm.BudgetExceeded as e:
            trace["steps"].append({"role": "pipeline", "text": f"STOP: {e}"}); break
        if msg.stop_reason == "refusal":
            trace["steps"].append({"role": "pipeline", "text": "refusal"}); break
        messages.append({"role": "assistant", "content": msg.content})
        texts = [b.text for b in msg.content if b.type == "text" and b.text.strip()]
        uses = [b for b in msg.content if b.type == "tool_use"]
        trace["steps"].append({"role": "agent", "text": "\n".join(texts),
                               "tool_calls": [{"name": u.name, "input": u.input} for u in uses]})
        if not uses:
            messages.append({"role": "user", "content": "Please use the tools or call submit."}); continue
        results = []
        for u in uses:
            inp = u.input if isinstance(u.input, dict) else json.loads(u.input)
            out_blocks, imgs = [], []
            try:
                if u.name == "look":
                    pad = float(np.clip(inp.get("pad", 0.35), 0.1, 2.0))
                    im = crop_full(cam, ts + timedelta(seconds=inp["offset_s"]), box, pad=pad)
                    out_blocks = [img_block(enc(im, 900))] if im is not None else [{"type": "text", "text": "no frame"}]
                    imgs.append(keep(im, f"look_{inp['offset_s']:+.0f}s"))
                elif u.name == "look_sequence":
                    for off in inp["offsets_s"][:4]:
                        im = whole_frame(cam, ts + timedelta(seconds=off), [("?", box, (0, 255, 0))])
                        out_blocks += [{"type": "text", "text": f"offset {off:+.0f}s (box shows the case-time position)"}, img_block(enc(im, 900))]
                        imgs.append(keep(im, f"seq_{off:+.0f}s"))
                elif u.name == "look_other_camera":
                    im = whole_frame(inp["camera"], ts + timedelta(seconds=inp.get("offset_s", 0)))
                    out_blocks = [img_block(enc(im))] if im is not None else [{"type": "text", "text": "no video for that camera/time"}]
                    imgs.append(keep(im, f"other_{inp['camera'].replace(' ','')}"))
                elif u.name == "chew_check":
                    r = chew_signal(cam, ts + timedelta(seconds=inp["offset_s"]), box)
                    out_blocks = [{"type": "text", "text": json.dumps(r)}]
                elif u.name == "submit":
                    issues = _geometry_check(case, inp)
                    if issues and not rechecked:
                        rechecked = True
                        out_blocks = [{"type": "text", "text": "SELF-CHECK FAILED: " + "; ".join(issues) +
                                       ". Look again (different offset or camera) and resubmit; keep your answer only if the new evidence clearly supports it."}]
                    else:
                        answer = dict(inp); answer["geometry_issues"] = issues; answer["rechecked"] = rechecked
                        out_blocks = [{"type": "text", "text": "recorded"}]
            except Exception as e:
                out_blocks = [{"type": "text", "text": f"tool error: {e}"}]
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": out_blocks})
            trace["steps"].append({"role": "tool", "name": u.name,
                                   "result": [b.get("text") for b in out_blocks if b["type"] == "text"], "images": imgs})
        messages.append({"role": "user", "content": results})
        if answer:
            break
    trace["answer"] = answer
    trace["rounds"] = rnd + 1
    if save:
        (TRACE_DIR / f"{case['case_id']}.json").write_text(json.dumps(trace, indent=1, default=str))
    return answer, trace
