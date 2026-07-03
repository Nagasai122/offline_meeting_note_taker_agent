"""
Unit tests for cli/feedback.py (Fix 3.4).

Verifies that record_rejection and record_edit write the correct JSONL
structure to the expected paths, include the meeting_type field (SB-5.1),
and are idempotent under repeated calls (append-only).
"""
from __future__ import annotations

import json

import pytest

from cli.feedback import record_edit, record_rejection


def test_record_rejection_creates_file_and_writes_correct_fields(tmp_path):
    feedback_dir = tmp_path / "feedback"
    record_rejection(
        session_id="standup-1",
        item_id="item-abc",
        item_description="Complete the quarterly report",
        rejection_reason="Not explicitly assigned in the transcript",
        feedback_dir=feedback_dir,
        meeting_type="is-call",
    )

    jsonl = feedback_dir / "rejections.jsonl"
    assert jsonl.exists(), "rejections.jsonl was not created"

    record = json.loads(jsonl.read_text().strip())
    assert record["session_id"] == "standup-1"
    assert record["item_id"] == "item-abc"
    assert record["item_description"] == "Complete the quarterly report"
    assert record["rejection_reason"] == "Not explicitly assigned in the transcript"
    assert record["meeting_type"] == "is-call"
    assert "at" in record  # ISO timestamp
    assert "id" in record  # UUID


def test_record_rejection_appends_multiple_records(tmp_path):
    feedback_dir = tmp_path / "feedback"
    for i in range(3):
        record_rejection(
            session_id=f"session-{i}",
            item_id=f"item-{i}",
            item_description=f"Description {i}",
            rejection_reason="",
            feedback_dir=feedback_dir,
        )

    lines = (feedback_dir / "rejections.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    records = [json.loads(l) for l in lines]
    assert [r["session_id"] for r in records] == ["session-0", "session-1", "session-2"]


def test_record_rejection_missing_meeting_type_defaults_to_empty(tmp_path):
    feedback_dir = tmp_path / "feedback"
    record_rejection(
        session_id="s",
        item_id="i",
        item_description="desc",
        rejection_reason="reason",
        feedback_dir=feedback_dir,
        # meeting_type not supplied — defaults to ""
    )
    record = json.loads((feedback_dir / "rejections.jsonl").read_text().strip())
    assert record["meeting_type"] == ""


def test_record_edit_creates_edits_file(tmp_path):
    feedback_dir = tmp_path / "feedback"
    record_edit(
        session_id="project-1",
        item_id="item-xyz",
        original="Fix the bug",
        corrected="Fix the authentication bug by Monday",
        feedback_dir=feedback_dir,
        meeting_type="project-meeting",
    )

    jsonl = feedback_dir / "edits.jsonl"
    assert jsonl.exists(), "edits.jsonl was not created"

    record = json.loads(jsonl.read_text().strip())
    assert record["session_id"] == "project-1"
    assert record["item_id"] == "item-xyz"
    assert record["original"] == "Fix the bug"
    assert record["corrected"] == "Fix the authentication bug by Monday"
    assert record["meeting_type"] == "project-meeting"
    assert "at" in record
    assert "id" in record


def test_record_edit_and_rejection_use_separate_files(tmp_path):
    """Edits and rejections must go to separate JSONL files — not the same one."""
    feedback_dir = tmp_path / "feedback"
    record_rejection(
        session_id="s", item_id="i1", item_description="d1",
        rejection_reason="r", feedback_dir=feedback_dir,
    )
    record_edit(
        session_id="s", item_id="i2", original="old", corrected="new",
        feedback_dir=feedback_dir,
    )
    assert (feedback_dir / "rejections.jsonl").exists()
    assert (feedback_dir / "edits.jsonl").exists()
    # Each file has exactly one record
    assert len((feedback_dir / "rejections.jsonl").read_text().strip().splitlines()) == 1
    assert len((feedback_dir / "edits.jsonl").read_text().strip().splitlines()) == 1


def test_record_rejection_creates_parent_dirs(tmp_path):
    """feedback_dir is created automatically if it does not exist."""
    deep = tmp_path / "a" / "b" / "c" / "feedback"
    assert not deep.exists()
    record_rejection(
        session_id="s", item_id="i", item_description="d",
        rejection_reason="", feedback_dir=deep,
    )
    assert (deep / "rejections.jsonl").exists()
