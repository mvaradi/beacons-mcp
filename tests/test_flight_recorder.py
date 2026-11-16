import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "flight_recorder.py"
TIMELINE = ROOT / "scripts" / "loop_timeline.py"
FIXTURE = ROOT / "tests" / "fixtures" / "flight_sample.jsonl"

BIG = "x" * 1000
EVENTS = [
    {"hook_event_name": "UserPromptSubmit", "prompt": "hello"},
    {"hook_event_name": "PreToolUse", "tool_name": "mcp__beacons__find_structures",
     "mcp_server": {"name": "beacons", "source": "project"}, "tool_use_id": "t1", "tool_input": {"a": 1}},
    {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": "t1",
     "tool_input": {}, "tool_response": {"text": BIG}, "duration_ms": 5},
    {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "t2",
     "tool_input": {}, "error": "Exit code 1\nboom", "duration_ms": 3},
    {"hook_event_name": "PostToolBatch", "tool_calls": [
        {"tool_name": "Read", "tool_use_id": "t1", "tool_input": {}, "tool_response": BIG}]},
    {"hook_event_name": "Elicitation", "mcp_server_name": "beacons", "message": "Pick one"},
    {"hook_event_name": "ElicitationResult", "mcp_server_name": "beacons", "action": "accept",
     "content": {"k": "v"}},
    {"hook_event_name": "Stop", "last_assistant_message": "done", "unknown_extra": 1},
]


def run_hook(tmp_path, stdin):
    env = dict(os.environ, FLIGHT_RECORDER_LOG_DIR=str(tmp_path))
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=10)


def test_each_event_appends_one_line(tmp_path):
    for ev in EVENTS:
        r = run_hook(tmp_path, json.dumps({"session_id": "sess", **ev}))
        assert r.returncode == 0 and r.stdout == ""
    lines = (tmp_path / "flight-sess.jsonl").read_text().splitlines()
    assert len(lines) == len(EVENTS)
    recs = [json.loads(line) for line in lines]
    assert recs[1]["mcp_server"] == "beacons"
    assert recs[2]["tool_response"]["chars"] > 1000
    assert len(recs[2]["tool_response"]["head"]) == 400
    assert recs[4]["tool_calls"][0]["tool_response"] == {"chars": 1000, "head": BIG[:400]}
    assert recs[5]["mcp_server"] == "beacons"


def test_malformed_input_is_harmless(tmp_path):
    for bad in ("not json", "", "[1,2]"):
        r = run_hook(tmp_path, bad)
        assert r.returncode == 0 and r.stdout == ""
    assert not list(tmp_path.glob("*.jsonl"))


def test_timeline_on_fixture():
    r = subprocess.run([sys.executable, str(TIMELINE), str(FIXTURE)], capture_output=True,
                       text=True, timeout=10)
    assert r.returncode == 0
    out = r.stdout
    assert "Model request #2" in out
    assert "ELICITATION" in out
    assert "FAILED" in out and "File not found" in out
    assert "PostToolUse=900 chars, model saw=1200 chars" in out
    for line in ("model requests: 2", "batches: 1", "tool calls: 2", "failures: 1",
                 "chars returned to the model (per PostToolBatch): 1220"):
        assert line in out


def test_non_ascii_is_read_and_written_as_utf8(tmp_path):
    """Regression: with a non-UTF-8 default encoding (Windows) 'Å' was logged as 'Ã…'."""
    env = dict(os.environ, FLIGHT_RECORDER_LOG_DIR=str(tmp_path), PYTHONUTF8="0",
               PYTHONIOENCODING="cp1252")
    payload = json.dumps({"session_id": "u", "hook_event_name": "UserPromptSubmit",
                          "prompt": "resolution 1.55 Å"}, ensure_ascii=False)
    r = subprocess.run([sys.executable, str(HOOK)], input=payload.encode("utf-8"),
                       capture_output=True, env=env, timeout=10)
    assert r.returncode == 0 and r.stderr == b""
    raw = (tmp_path / "flight-u.jsonl").read_bytes()
    assert "1.55 Å".encode("utf-8") in raw
    assert "Ã…".encode("utf-8") not in raw
