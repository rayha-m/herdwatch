"""Render agent reasoning traces (JSON) as readable Markdown, and pick the three the brief asks for.

    python tools/render_traces.py --run full
Writes outputs/level1/<run>/agent_traces/TRACES.md with every trace, and marks:
  SUCCESS: highest-confidence daytime behaviour case where the agent's answer survived the self-check
  FAILURE: a case where the agent could not decide (low confidence) or the self-check bounced it
  NIGHT:   a night (IR) case
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.config import OUT


def render(t):
    c = t["case"]; a = t.get("answer") or {}
    out = [f"### {c['case_id']}  ({c['kind']}, {c['cam']}, {c['ts']})", "", f"**Question:** {c['question']}", "",
           f"**Tracker context:** {c.get('context','')}", ""]
    for i, s in enumerate(t["steps"]):
        if s["role"] == "agent":
            calls = "; ".join(f"`{x['name']}({json.dumps(x['input'])[:160]})`" for x in s.get("tool_calls", []) if x["name"] != "submit")
            if s.get("text"):
                out.append(f"- **agent thinks:** {s['text'][:600]}")
            if calls:
                out.append(f"- **agent calls:** {calls}")
            for x in s.get("tool_calls", []):
                if x["name"] == "submit":
                    out.append(f"- **agent submits:** `{x['input'].get('behavior')}` / posture `{x['input'].get('posture')}` / "
                               f"id `{x['input'].get('same_cow_as_candidate')}` / conf {x['input'].get('confidence')}")
        elif s["role"] == "tool":
            res = " ".join(r for r in s.get("result", []) if r)[:300]
            if s["name"] == "submit" and "SELF-CHECK FAILED" in res:
                out.append(f"- **pipeline self-check:** {res}")
            elif res and s["name"] != "submit":
                out.append(f"- *{s['name']} returned:* {res}")
            elif s.get("images"):
                out.append(f"- *{s['name']} returned {len(s['images'])} image(s)*")
    if a:
        out += ["", f"**Final:** {a.get('behavior')} (posture {a.get('posture')}, confidence {a.get('confidence')}, "
                    f"clinical signs {a.get('clinical_signs')}, re-checked after self-check: {a.get('rechecked')})",
                f"**Evidence:** {a.get('evidence','')}", f"**Self-check:** {a.get('self_check','')}"]
    else:
        out += ["", "**Final:** no answer (budget stop or no submit)"]
    out.append(f"\n_{t.get('rounds')} model round(s)._\n")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--run", default="full"); a = ap.parse_args()
    d = OUT / "level1" / a.run / "agent_traces"
    T = [json.load(open(p)) for p in sorted(d.glob("*.json"))]
    def conf(t): return (t.get("answer") or {}).get("confidence", 0) or 0
    def night(t): return "night" in t["case"]["kind"]
    day = [t for t in T if not night(t) and t.get("answer") and not t["answer"].get("geometry_issues") and t["case"]["kind"].startswith("behaviour")]
    succ = max(day, key=conf) if day else None
    fails = [t for t in T if t.get("answer") is None or conf(t) < 0.45 or (t.get("answer") or {}).get("rechecked")]
    fail = min(fails, key=conf) if fails else None
    nights = [t for t in T if night(t) and t.get("answer")]
    nt = max(nights, key=conf) if nights else None
    lines = ["# Agent reasoning traces", "", f"{len(T)} hard cases. Images the agent looked at are kept locally (event data) and not committed.", "",
             "## The three required traces", ""]
    for lab, t in (("SUCCESS", succ), ("FAILURE", fail), ("NIGHT", nt)):
        if t:
            lines += [f"## {lab}", render(t)]
    lines += ["## All traces", ""] + [render(t) for t in T]
    (d / "TRACES.md").write_text("\n".join(lines))
    print("wrote", d / "TRACES.md", "| success:", succ and succ["case"]["case_id"], "| failure:", fail and fail["case"]["case_id"], "| night:", nt and nt["case"]["case_id"])


if __name__ == "__main__":
    main()
