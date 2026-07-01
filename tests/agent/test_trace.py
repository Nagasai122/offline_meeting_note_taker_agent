from __future__ import annotations

import json

from agent.trace import TraceLogger, TraceStep


def test_trace_writes_one_jsonl_record_per_event(tmp_path):
    trace = TraceLogger(tmp_path, "run-1")
    trace.start("Drive session 's1' to PROPOSED.", "s1")
    trace.step(TraceStep(iteration=1, thought="check status", action="get_session_status", arguments={"session_id": "s1"}, observation={"state": "RECORDING"}))
    trace.finish("final", "done")

    lines = trace.path.read_text().strip().splitlines()
    assert len(lines) == 3

    records = [json.loads(line) for line in lines]
    assert records[0]["event"] == "run_started"
    assert records[0]["session_id"] == "s1"
    assert records[1]["event"] == "step"
    assert records[1]["action"] == "get_session_status"
    assert records[1]["observation"] == {"state": "RECORDING"}
    assert records[2]["event"] == "run_finished"
    assert records[2]["outcome"] == "final"


def test_trace_path_is_under_trace_dir_and_named_by_run_id(tmp_path):
    trace = TraceLogger(tmp_path / "nested", "abc-123")
    assert trace.path == tmp_path / "nested" / "abc-123.jsonl"
    trace.start("goal", None)
    assert trace.path.exists()


def test_trace_survives_non_json_native_observation_values(tmp_path):
    trace = TraceLogger(tmp_path, "run-2")
    trace.step(TraceStep(iteration=1, thought="x", action="y", arguments={}, error=RuntimeError("boom")))
    record = json.loads(trace.path.read_text().strip().splitlines()[0])
    assert "boom" in record["error"]
