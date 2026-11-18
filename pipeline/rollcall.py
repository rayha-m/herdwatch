"""Agent roll call: describe every visible cow at a moment, for identity re-anchoring.

The bulk tracker keeps identities well over minutes to hours, but drifts over two days
(swaps when the herd crowds at the far bunk). Ear-tag colours and white markings
separate most cows, but they are only readable on zoomed full-resolution crops, which
pixel-colour features cannot do reliably at Cam 03 distances (the yellow waterer bleeds
into boxes). So at a few daylight "roll-call" moments the agent receives a zoomed crop
of every cow on Cam 03, numbered, and returns structured attributes per cow.
"""
import json, base64
import numpy as np, pandas as pd, cv2
from . import agent as ag
from . import llm

ATTR_TOOL = {
    "name": "submit_roll_call",
    "description": "One entry per numbered crop.",
    "strict": True,
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["cows"], "properties": {
        "cows": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                 "required": ["crop", "tag_colours", "white_markings", "other_marks", "visible_quality"],
                 "properties": {
                     "crop": {"type": "integer"},
                     "tag_colours": {"type": "array", "items": {"type": "string", "enum": ["yellow", "green", "blue", "red", "pink", "orange", "white", "none_visible"]}},
                     "white_markings": {"type": "string", "enum": ["none", "white_face", "white_blaze_or_patch_on_face", "white_on_neck_or_chest", "white_on_body", "not_visible"]},
                     "other_marks": {"type": "string", "description": "paint/brand/scar/missing tag, colour shade (black vs brownish), body size; short"},
                     "visible_quality": {"type": "string", "enum": ["good", "partial", "poor"]}}}}}},
}

PROMPT = ("Numbered zoomed crops of the cows in Pen 6 at one moment (black Angus; some have white face markings). "
          "For EACH crop describe only what you can see that could identify this cow later: ear-tag colour(s), white "
          "markings, other marks (paint, brand, scar, brownish coat, size). Use not_visible / none_visible when you "
          "cannot see the head. Call submit_roll_call once.")


def roll_call(samples_at_t, cam="Cam 03", max_cows=10):
    g = samples_at_t.sort_values("x1").head(max_cows)
    content = [{"type": "text", "text": PROMPT}]
    ids = []
    for k, r in enumerate(g.itertuples(), 1):
        im = ag.crop_full(cam, r.ts, [r.x1, r.y1, r.x2, r.y2], pad=0.25)
        content += [{"type": "text", "text": f"crop {k}"}, ag.img_block(ag.enc(im, 520))]
        ids.append(int(r.cow))
    msg = llm.create("rollcall", max_tokens=6000, tools=[ATTR_TOOL], output_config={"effort": "low"},
                     messages=[{"role": "user", "content": content}])
    out = []
    for b in msg.content:
        if b.type == "tool_use" and b.name == "submit_roll_call":
            for c in b.input["cows"]:
                if 1 <= c["crop"] <= len(ids):
                    out.append(dict(c, tracker_cow=ids[c["crop"] - 1], ts=str(g.ts.iloc[0])))
    return out
