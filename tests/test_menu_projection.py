"""Privacy regressions against Hermes' real compaction and human-turn APIs."""

from copy import deepcopy

import pytest
from agent.context_compressor import (
    _SUMMARY_END_MARKER,
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    ContextCompressor,
    is_compaction_summary_message,
    user_originated_turn_view,
)

from hermes_lark_streaming.bot_menu import plain_messages

ASK = "Which files should I review next?"
SECRET = "PRIVATE compaction notes: do not render this handoff"


@pytest.fixture
def native_carrier():
    def make(*, multipart, force_user_leading, metadata):
        content = [{"type": "text", "text": ASK}] if multipart else ASK
        row = {"role": "user", "content": content}
        # Exercise the actual native carrier writer, without initializing an LLM.
        compressor = object.__new__(ContextCompressor)
        compressor._summary_has_user_turn = True
        compressor._merge_summary_into_tail_row(
            row, SUMMARY_PREFIX + "\n" + SECRET, "user", force_user_leading
        )
        if not metadata:
            row = {"role": row["role"], "content": row["content"]}
        else:
            # Native persistence uses this legacy wrapper on summary rows.
            row["display_kind"] = "hidden"
        assert is_compaction_summary_message(row)
        view = user_originated_turn_view(row)
        assert view is not None
        assert SECRET not in str(view["content"])
        assert ASK in str(view["content"])
        return row

    return make


@pytest.mark.parametrize("multipart", [False, True], ids=["text", "blocks"])
@pytest.mark.parametrize("force_user_leading", [False, True], ids=["suffix", "prefix"])
@pytest.mark.parametrize("metadata", [False, True], ids=["wire-stripped", "flagged-hidden"])
def test_native_composite_renders_only_live_human_ask(native_carrier, multipart, force_user_leading, metadata):
    row = native_carrier(multipart=multipart, force_user_leading=force_user_leading, metadata=metadata)
    original = deepcopy(row)
    assert plain_messages([row]) == [("user", ASK)]
    assert row == original


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("metadata", [False, True])
def test_standalone_native_summary_is_never_rendered(role, metadata):
    row = {"role": role, "content": SUMMARY_PREFIX + "\n" + SECRET + "\n\n" + _SUMMARY_END_MARKER}
    if metadata:
        row[COMPRESSED_SUMMARY_METADATA_KEY] = True
    assert is_compaction_summary_message(row)
    assert plain_messages([row]) == []


def test_projection_keeps_public_text_not_internal_rows_or_reasoning():
    rows = [
        {"role": "system", "content": SECRET},
        {"role": "tool", "content": SECRET},
        {"role": "user", "content": SECRET, "hidden": True},
        {"role": "user", "content": SECRET, "display_kind": "hidden"},
        {"role": "assistant", "content": SECRET, "is_summary": True},
        {"role": "user", "content": SECRET, "_compressed_summary": True},
        {"role": "assistant", "content": SECRET, "tool_calls": [{"id": "internal"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": SECRET}]},
        {"role": "user", "content": ASK},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": SECRET},
            {"type": "text", "text": "Review the tests."},
        ], "reasoning": SECRET},
    ]
    assert plain_messages(rows) == [("user", ASK), ("assistant", "Review the tests.")]
