"""Unit tests for self-validating trace logger (§7.6)."""

import pytest
from agent.trace_logger import TraceLogger, TraceValidationError
from agent.schemas.actions import ToolCallAction, ToolCancelAction, FillerAction


def test_trace_logger_valid_flow():
    tracer = TraceLogger()
    sid = "sess_trace_1"

    # Action 1: Filler in epoch 1
    tracer.log_action(FillerAction(session_id=sid, epoch=1, text="Looking up..."))

    # Action 2: Tool call in epoch 1
    call = ToolCallAction(
        session_id=sid,
        epoch=1,
        call_id="call_x1",
        tool_name="test_tool",
        arguments={},
    )
    tracer.log_action(call)

    # Action 3: Cancel tool in epoch 2
    cancel = ToolCancelAction(
        session_id=sid,
        epoch=2,
        call_id="call_x1",
        tool_name="test_tool",
        reason="interrupted",
    )
    tracer.log_action(cancel)
    assert len(tracer.trace_history) == 3


def test_trace_logger_catches_epoch_regression():
    tracer = TraceLogger()
    sid = "sess_trace_2"

    tracer.log_action(FillerAction(session_id=sid, epoch=3, text="Fast response"))

    # Attempt to log an action with an older epoch (3 -> 2)
    with pytest.raises(TraceValidationError, match="Epoch regression detected"):
        tracer.log_action(FillerAction(session_id=sid, epoch=2, text="Regression!"))


def test_trace_logger_catches_unknown_call_cancel():
    tracer = TraceLogger()
    sid = "sess_trace_3"

    # Attempt to cancel call_id that was never dispatched
    with pytest.raises(TraceValidationError, match="Tool cancellation for unknown call_id"):
        tracer.log_action(
            ToolCancelAction(
                session_id=sid,
                epoch=1,
                call_id="ghost_call_999",
                tool_name="ghost_tool",
                reason="interrupt",
            )
        )


def test_trace_logger_rejects_malformed_schema_in_strict_mode():
    """A record missing a required field (empty session_id) must fail schema validation, not just invariants."""
    tracer = TraceLogger(strict=True)
    with pytest.raises(TraceValidationError, match="schema validation"):
        tracer.log_action(FillerAction(session_id="", epoch=1, text="no session id"))


def test_trace_logger_drops_and_counts_malformed_records_in_non_strict_mode():
    """§7.6: eval/prod mode drops a bad record and counts it instead of taking the session down."""
    tracer = TraceLogger(strict=False)
    result = tracer.log_action(FillerAction(session_id="", epoch=1, text="no session id"))
    assert result is None
    assert tracer.dropped_count == 1
    assert len(tracer.trace_history) == 0

    # A valid record right after still logs normally.
    tracer.log_action(FillerAction(session_id="sess_ok", epoch=1, text="fine"))
    assert len(tracer.trace_history) == 1
    assert tracer.dropped_count == 1
