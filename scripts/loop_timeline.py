#!/usr/bin/env python3
"""Print a human-readable timeline of a flight-recorder log.

Usage: loop_timeline.py [flight-<session>.jsonl]   (default: newest log)
"""
import json
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[1] / ".claude" / "logs"


def load(path):
    events = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print(f"(skipping malformed line {n})", file=sys.stderr)
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def chars(field):
    return field.get("chars") if isinstance(field, dict) else None


def short(tid):
    return (tid or "?")[-6:]


def fmt_ms(ms):
    return f"{ms}ms" if ms is not None else "?ms"


def name(ev):
    srv = ev.get("mcp_server")
    return f"{srv}:{ev.get('tool_name')}" if srv else str(ev.get("tool_name"))


def render(events, out=print):
    turn = batches = calls = failures = returned = elicits = 0
    post = {}  # tool_use_id -> PostToolUse / PostToolUseFailure event

    def open_turn(why):
        nonlocal turn
        turn += 1
        out(f"\n== Model request #{turn} ({why}) ==")

    for ev in events:
        kind = ev.get("hook_event_name")
        ts = (ev.get("timestamp") or "")[11:23]
        if kind == "UserPromptSubmit":
            p = ev.get("prompt") or {}
            open_turn("user prompt")
            out(f"  {ts} prompt ({chars(p)} chars): {(p.get('head') or '')[:100]!r}")
        elif kind == "PreToolUse":
            out(f"  {ts} -> [{short(ev.get('tool_use_id'))}] {name(ev)}")
        elif kind in ("PostToolUse", "PostToolUseFailure"):
            tid = ev.get("tool_use_id")
            post[tid] = ev
            calls += 1
            if kind == "PostToolUseFailure":
                failures += 1
                err = (ev.get("error") or {}).get("head", "")
                out(f"  {ts} <- [{short(tid)}] {name(ev)} FAILED {fmt_ms(ev.get('duration_ms'))}: {err[:200]!r}")
            else:
                out(f"  {ts} <- [{short(tid)}] {name(ev)} ok {fmt_ms(ev.get('duration_ms'))}")
        elif kind == "Elicitation":
            elicits += 1
            m = ev.get("message") or {}
            out(f"  {ts} ** ELICITATION from {ev.get('mcp_server')}: {(m.get('head') or '')[:100]!r}")
        elif kind == "ElicitationResult":
            out(f"  {ts} ** ELICITATION RESULT from {ev.get('mcp_server')}: action={ev.get('action')}")
        elif kind == "PostToolBatch":
            batches += 1
            entries = ev.get("tool_calls") or []
            out(f"  {ts} batch #{batches}: {len(entries)} tool call(s)")
            for c in entries:
                tid = c.get("tool_use_id")
                seen = chars(c.get("tool_response"))
                p = post.get(tid, {})
                if p.get("hook_event_name") == "PostToolUseFailure":
                    structured = f"error {chars(p.get('error'))}"
                else:
                    structured = chars(p.get("tool_response"))
                if seen is not None:
                    returned += seen
                elif chars(p.get("error")):
                    returned += chars(p["error"])
                out(f"      [{short(tid)}] {c.get('tool_name')}: PostToolUse={structured} chars, "
                    f"model saw={seen} chars")
            open_turn("after tool batch")
        elif kind == "Stop":
            m = ev.get("last_assistant_message") or {}
            out(f"  {ts} stop (final message {chars(m)} chars)")
    out("\n== Totals ==")
    out(f"  model requests: {turn}")
    out(f"  batches: {batches}")
    out(f"  tool calls: {calls}")
    out(f"  failures: {failures}")
    out(f"  elicitations: {elicits}")
    out(f"  chars returned to the model (per PostToolBatch): {returned}")


def main(argv):
    if len(argv) > 1:
        path = Path(argv[1])
    else:
        logs = sorted(LOG_DIR.glob("flight-*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not logs:
            print(f"No logs in {LOG_DIR}. Hooks only run inside Claude Code; send a prompt first.",
                  file=sys.stderr)
            return 1
        path = logs[-1]
    print(f"Timeline for {path}")
    render(load(path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
