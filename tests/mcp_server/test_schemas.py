from __future__ import annotations

import pytest

from mcp_server.schemas import SchemaValidationError, validate_session_id


@pytest.mark.parametrize("session_id", ["standup-2026-06-30", "abc_123", "a", "A1-B2_c3"])
def test_valid_session_ids_pass(session_id):
    validate_session_id(session_id)  # should not raise


@pytest.mark.parametrize(
    "session_id",
    ["../../etc/passwd", "has space", "has/slash", "", "x" * 129, "semi;colon", None, 123],
)
def test_invalid_session_ids_raise(session_id):
    with pytest.raises(SchemaValidationError):
        validate_session_id(session_id)
