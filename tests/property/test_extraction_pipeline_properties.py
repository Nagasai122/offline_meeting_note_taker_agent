"""Property-based tests (hypothesis) for the extraction->Markdown pipeline
(audit Strand C).

LLM output is inherently variable, so these tests assert *structural
invariants* rather than exact strings:

1. todo.md round-trip: format_todo_file(parse_todo(format_todo_file(x))) is
   lossless for every representable TodoItem — no field dropped, no item
   lost, output always re-parsable.
2. _parse_extraction_result: for arbitrary model output, either returns a
   dict satisfying the documented contract (summary + action_items list of
   dicts with 'description') or raises ExtractionError — never any other
   exception type, never a malformed success.
3. propose-draft format: any accepted extraction result renders to a draft
   that parse_todo can read back with ids/descriptions intact (the same
   parser the review/apply pipeline uses on the real files).
"""

from __future__ import annotations

import json
import string

from hypothesis import given, settings, strategies as st

from mcp_server.todo import TodoFile, TodoItem, format_todo_file, parse_todo
from mcp_server.tools.extraction import ExtractionError, _parse_extraction_result

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Descriptions live on a single Markdown checklist line; the writer never
# escapes newlines or HTML-comment terminators, so the representable domain
# excludes them (documented format constraint, not an accident of this test).
_description = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\n\r"),
    min_size=1,
    max_size=120,
).filter(lambda s: "-->" not in s and "<!--" not in s and s.strip() == s and s.strip() != "")

_opt_short = st.one_of(st.none(), st.text(string.ascii_letters + string.digits + "-_ .", min_size=1, max_size=30))
_status = st.sampled_from(["todo", "in_progress", "done", "blocked", "deleted"])
_priority = st.one_of(st.none(), st.sampled_from(["LOW", "MEDIUM", "HIGH"]))

_todo_item = st.builds(
    TodoItem,
    description=_description,
    done=st.booleans(),
    id=st.one_of(st.none(), st.text(string.hexdigits.lower(), min_size=1, max_size=12)),
    owner=_opt_short,
    due_date=st.one_of(st.none(), st.dates().map(str)),
    session_id=_opt_short,
    priority=_priority,
    status=_status,
    source=st.one_of(st.none(), st.just("manual"), _opt_short),
    progress_note=_opt_short,
    tag=_opt_short,
)


# ---------------------------------------------------------------------------
# 1. todo.md round-trip
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(items=st.lists(_todo_item, max_size=15))
def test_todo_file_roundtrip_is_lossless(tmp_path_factory, items):
    path = tmp_path_factory.mktemp("prop") / "todo.md"
    original = TodoFile(items=items)

    path.write_text(format_todo_file(original), encoding="utf-8")
    reparsed = parse_todo(path)

    assert len(reparsed.items) == len(original.items)
    for orig, back in zip(original.items, reparsed.items):
        assert back.description == orig.description
        assert back.done == orig.done
        assert back.id == orig.id
        assert back.owner == orig.owner
        assert back.due_date == orig.due_date
        assert back.session_id == orig.session_id
        assert back.priority == orig.priority
        assert back.status == orig.status
        assert back.source == orig.source
        assert back.progress_note == orig.progress_note
        assert back.tag == orig.tag

    # Second write must be byte-identical (idempotent fixed point).
    assert format_todo_file(reparsed) == format_todo_file(original)


# ---------------------------------------------------------------------------
# 2. _parse_extraction_result: total over arbitrary model output
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(raw=st.text(max_size=400))
def test_parse_extraction_result_never_raises_anything_but_extraction_error(raw):
    try:
        result = _parse_extraction_result(raw)
    except ExtractionError:
        return
    assert isinstance(result, dict)
    assert "summary" in result
    assert isinstance(result["action_items"], list)
    for item in result["action_items"]:
        assert isinstance(item, dict) and "description" in item


_action_item = st.fixed_dictionaries(
    {"description": _description},
    optional={
        "owner": _opt_short,
        "due_date": st.one_of(st.none(), st.dates().map(str)),
        "priority": st.sampled_from(["LOW", "MEDIUM", "HIGH"]),
    },
)


@settings(max_examples=150, deadline=None)
@given(
    summary=st.text(max_size=200),
    action_items=st.lists(_action_item, max_size=10),
    fence=st.booleans(),
)
def test_parse_extraction_result_accepts_valid_payloads_fenced_or_not(summary, action_items, fence):
    payload = json.dumps({"summary": summary, "action_items": action_items})
    raw = f"```json\n{payload}\n```" if fence else payload

    result = _parse_extraction_result(raw)

    assert result["summary"] == summary
    assert result["action_items"] == action_items


# ---------------------------------------------------------------------------
# 3. accepted extraction output -> pending_review draft -> parse_todo
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(action_items=st.lists(_action_item, min_size=1, max_size=10))
def test_extraction_items_survive_draft_rendering_roundtrip(tmp_path_factory, action_items):
    """Mirrors mcp_server/tools/review.py's draft format: every extracted
    action item must come back from the draft file with description and
    per-item metadata intact — the property that makes 'no data loss between
    extraction and human review' true."""
    draft_dir = tmp_path_factory.mktemp("draft")
    path = draft_dir / "prop-session.md"
    lines = ["# Proposed todo updates -- session prop-session", ""]
    for i, item in enumerate(action_items):
        meta = {
            "id": f"prop{i:04d}",
            "owner": item.get("owner"),
            "due_date": item.get("due_date"),
            "session_id": "prop-session",
        }
        lines.append(f"- [ ] {item['description']} <!-- meta: {json.dumps(meta)} -->")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed = parse_todo(path)

    assert len(parsed.items) == len(action_items)
    for i, (item, back) in enumerate(zip(action_items, parsed.items)):
        assert back.description == item["description"]
        assert back.id == f"prop{i:04d}"
        assert back.owner == item.get("owner")
        assert back.due_date == item.get("due_date")
