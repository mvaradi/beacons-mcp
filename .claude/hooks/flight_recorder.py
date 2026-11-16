#!/usr/bin/env python3
"""Flight recorder: append one JSON line per Claude Code hook event.

Stdlib only. Prints nothing to stdout (UserPromptSubmit stdout would be injected
into the model's context), always exits 0, never blocks, logs its own errors to
stderr only. Field names follow https://code.claude.com/docs/en/hooks; absent
fields are simply omitted.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HEAD = 400
# Fields holding a tool result / message / content: logged as {chars, head}.
BULKY = (
    "tool_response", "error", "prompt", "message", "content",
    "last_assistant_message", "requested_schema",
)


def summarize(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return {"chars": len(text), "head": text[:HEAD]}


def server_name(value):
    # PreToolUse/PostToolUse: object with "name"; Elicitation*: "mcp_server_name".
    if isinstance(value, dict):
        return value.get("name")
    return value


def record(data):
    rec = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "hook_event_name": data.get("hook_event_name"),
    }
    for key in ("session_id", "prompt_id", "agent_id", "tool_name", "tool_use_id",
                "tool_input", "duration_ms", "is_interrupt", "action", "mode",
                "elicitation_id", "stop_hook_active"):
        if key in data:
            rec[key] = data[key]
    srv = server_name(data.get("mcp_server", data.get("mcp_server_name")))
    if srv is not None:
        rec["mcp_server"] = srv
    for key in BULKY:
        if key in data and data[key] is not None:
            rec[key] = summarize(data[key])
    calls = data.get("tool_calls")
    if isinstance(calls, list):
        rec["tool_calls"] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            entry = {k: call[k] for k in ("tool_name", "tool_use_id", "tool_input") if k in call}
            srv = server_name(call.get("mcp_server"))
            if srv is not None:
                entry["mcp_server"] = srv
            if call.get("tool_response") is not None:
                entry["tool_response"] = summarize(call["tool_response"])
            rec["tool_calls"].append(entry)
    return rec


def log_dir():
    override = os.environ.get("FLIGHT_RECORDER_LOG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "logs"


def main():
    try:
        # Hooks send UTF-8 JSON; the Windows console code page would turn Å into 'Ã…'.
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
        sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(data.get("session_id") or "unknown"))
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record(data), ensure_ascii=False, default=str)
        with open(d / f"flight-{sid}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # never disturb the agent loop
        print(f"flight_recorder: {exc!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
