"""
JSONL trace logging for one agent run, under data/traces/<run_id>.jsonl.

Plain-file storage, consistent with the rest of this project. Every line is
one JSON record so a partially-written trace from a killed process is still
readable up to its last complete line (no single giant JSON document to
corrupt). Auditability is the point: this is the artefact a human reviews to
understand *why* the agent made each tool call, not just what it called --
directly serving the "draft-only, full human supervision" constraint at the
orchestration layer, the same way pending_review/*.md serves it at the
todo-write layer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TraceStep:
    iteration: int
    thought: str
    action: str
    arguments: dict
    observation: Any = None
    error: str | None = None
    at: str = field(default_factory=_now)


class TraceLogger:
    def __init__(self, trace_dir: Path | str, run_id: str) -> None:
        self.run_id = run_id
        self.path = Path(trace_dir) / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def start(self, goal: str, session_id: str | None) -> None:
        self._append({"event": "run_started", "at": _now(), "run_id": self.run_id, "goal": goal, "session_id": session_id})

    def step(self, step: TraceStep) -> None:
        self._append({"event": "step", **asdict(step)})

    def finish(self, outcome: str, detail: str | None = None) -> None:
        self._append({"event": "run_finished", "at": _now(), "run_id": self.run_id, "outcome": outcome, "detail": detail})

    def _append(self, record: dict) -> None:
        # default=str guards against any non-JSON-native value (e.g. an
        # exception instance) ending up in an observation field and crashing
        # the logger itself -- the trace must never be the thing that fails.
        with open(self.path, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
