"""Tests for the stream-json parser (SPEC §10.3-10.4)."""

from __future__ import annotations

import json
from typing import Any

from symphony.stream_parser import (
    AgentEvent,
    AgentEventType,
    parse_event,
    parse_stream,
)


def _line(**fields: Any) -> str:
    """Serialize a CLI event object to one stream-json line."""
    return json.dumps(fields)


# --- system events (§10.4) ----------------------------------------------------
def test_init_maps_to_session_started() -> None:
    event = parse_event(
        _line(type="system", subtype="init", session_id="sess-1", model="opus")
    )
    assert event.type is AgentEventType.SESSION_STARTED
    assert event.session_id == "sess-1"
    assert event.subtype == "init"
    assert event.is_terminal is False
    assert event.raw["model"] == "opus"


def test_api_retry_maps_to_api_retry() -> None:
    event = parse_event(
        _line(type="system", subtype="api_retry", error="rate_limit", attempt=2)
    )
    assert event.type is AgentEventType.API_RETRY
    assert event.raw["error"] == "rate_limit"


def test_unknown_system_subtype_is_other_message() -> None:
    event = parse_event(_line(type="system", subtype="something_new"))
    assert event.type is AgentEventType.OTHER_MESSAGE


# --- result events (§10.3) ----------------------------------------------------
def test_result_success_maps_to_turn_completed() -> None:
    event = parse_event(
        _line(type="result", subtype="success", is_error=False, session_id="sess-1")
    )
    assert event.type is AgentEventType.TURN_COMPLETED
    assert event.is_terminal is True
    assert event.session_id == "sess-1"


def test_result_error_subtype_maps_to_turn_failed() -> None:
    event = parse_event(_line(type="result", subtype="error_max_turns", is_error=True))
    assert event.type is AgentEventType.TURN_FAILED
    assert event.is_terminal is True


def test_result_success_subtype_but_is_error_is_failure() -> None:
    # is_error overrides a "success" subtype (SPEC §10.3).
    event = parse_event(_line(type="result", subtype="success", is_error=True))
    assert event.type is AgentEventType.TURN_FAILED


def test_result_without_subtype_is_failure() -> None:
    event = parse_event(_line(type="result", is_error=False))
    assert event.type is AgentEventType.TURN_FAILED
    assert event.is_terminal is True


# --- messages / unknown types -------------------------------------------------
def test_assistant_maps_to_notification() -> None:
    assert parse_event(_line(type="assistant")).type is AgentEventType.NOTIFICATION


def test_user_maps_to_other_message() -> None:
    assert parse_event(_line(type="user")).type is AgentEventType.OTHER_MESSAGE


def test_stream_event_maps_to_other_message() -> None:
    assert parse_event(_line(type="stream_event")).type is AgentEventType.OTHER_MESSAGE


def test_unknown_type_is_tolerated_as_other_message() -> None:
    # Forward-compatibility: unrecognized types must not abort (SPEC §10.3).
    assert parse_event(_line(type="brand_new_event")).type is (
        AgentEventType.OTHER_MESSAGE
    )


# --- malformed handling (§10.4) -----------------------------------------------
def test_non_json_line_is_malformed() -> None:
    event = parse_event("this is not json")
    assert event.type is AgentEventType.MALFORMED
    assert event.raw == {}
    assert event.line == "this is not json"


def test_json_non_object_is_malformed() -> None:
    # Valid JSON but not an event object — unusable as a protocol event.
    assert parse_event("[1, 2, 3]").type is AgentEventType.MALFORMED


def test_non_string_subtype_is_ignored() -> None:
    event = parse_event(_line(type="system", subtype=123))
    assert event.subtype is None
    assert event.type is AgentEventType.OTHER_MESSAGE


# --- stream iteration (§10.3) -------------------------------------------------
def test_parse_stream_classifies_a_full_turn_in_order() -> None:
    lines = [
        _line(type="system", subtype="init", session_id="sess-1"),
        _line(type="assistant"),
        _line(type="result", subtype="success", is_error=False, session_id="sess-1"),
    ]
    events = list(parse_stream(lines))
    assert [event.type for event in events] == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.NOTIFICATION,
        AgentEventType.TURN_COMPLETED,
    ]


def test_parse_stream_skips_blank_lines() -> None:
    lines = ["", "  ", _line(type="assistant"), "\n"]
    events = list(parse_stream(lines))
    assert len(events) == 1
    assert events[0].type is AgentEventType.NOTIFICATION


def test_parse_stream_continues_past_a_malformed_line() -> None:
    lines = [
        _line(type="system", subtype="init", session_id="s"),
        "GARBAGE not json",
        _line(type="result", subtype="success", is_error=False),
    ]
    events = list(parse_stream(lines))
    assert [event.type for event in events] == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.MALFORMED,
        AgentEventType.TURN_COMPLETED,
    ]


def test_default_event_has_empty_raw() -> None:
    # The raw default factory yields an independent dict per instance.
    first = AgentEvent(AgentEventType.MALFORMED)
    second = AgentEvent(AgentEventType.MALFORMED)
    assert first.raw == {} and second.raw == {}
    assert first.raw is not second.raw
