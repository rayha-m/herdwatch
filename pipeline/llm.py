"""Claude client with a hard call budget and a usage ledger.

The key is read from the environment or from .env (either `ANTHROPIC_API_KEY=...`
or a bare key on one line). Every call is appended to work/agent_usage.jsonl and
counted; once BV_MAX_AGENT_CALLS (default 400) is reached, calls raise
BudgetExceeded and the pipeline falls back to the YOLO/rule-based label.
"""
import json, os, time, threading
from pathlib import Path
import anthropic
from .config import ROOT, WORK, CLAUDE_MODEL

MAX_CALLS = int(os.environ.get("BV_MAX_AGENT_CALLS", "400"))
LEDGER = WORK / "agent_usage.jsonl"
_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


_exhausted = None   # set once the API reports no credit, so later calls fail fast


def _load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip().removeprefix("export ").strip() == "ANTHROPIC_API_KEY":
                    return v.strip().strip('"').strip("'")
            elif line.startswith("sk-ant-"):
                return line
    return None


_client = None


def client():
    global _client
    if _client is None:
        key = _load_key()
        if not key:
            raise RuntimeError("No ANTHROPIC_API_KEY in env or .env")
        _client = anthropic.Anthropic(api_key=key, max_retries=4, timeout=300)
    return _client


def calls_used():
    if not LEDGER.exists():
        return 0
    return sum(1 for _ in LEDGER.open())


def create(purpose, **kw):
    """messages.create with budget enforcement + ledger. Returns the Message."""
    with _lock:
        used = calls_used()
        if used >= MAX_CALLS:
            raise BudgetExceeded(f"agent call budget reached ({used}/{MAX_CALLS})")
    global _exhausted
    if _exhausted:
        raise BudgetExceeded(_exhausted)
    kw.setdefault("model", CLAUDE_MODEL)
    t = time.time()
    try:
        msg = client().messages.create(**kw)
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            _exhausted = "Anthropic API credit exhausted"
            raise BudgetExceeded(_exhausted) from None
        raise
    u = msg.usage
    with _lock:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), purpose=purpose, model=msg.model,
                                    input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                                    cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                                    stop=msg.stop_reason, secs=round(time.time() - t, 1))) + "\n")
    return msg


if __name__ == "__main__":
    m = create("smoke_test", max_tokens=200, output_config={"effort": "low"},
               messages=[{"role": "user", "content": "Reply with the single word: ok"}])
    print(m.stop_reason, [b.text for b in m.content if b.type == "text"], "calls used:", calls_used())
