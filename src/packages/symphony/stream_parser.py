"""Parse Claude ``stream-json`` stdout into normalized events (SPEC §10.3-10.4).

Each turn's subprocess emits newline-delimited JSON on stdout, one event per line,
ending with a terminal ``result`` event (SPEC §10.3). This module turns each line
into a normalized :class:`AgentEvent`: it parses the JSON, classifies the CLI event
type/subtype into a stable :class:`AgentEventType`, and carries the raw parsed
object for downstream use.

Two boundaries keep this focused on *parsing and classification*:

* **Forward-compatible tolerance (§10.3).** Unrecognized event types/subtypes map to
  :attr:`AgentEventType.OTHER_MESSAGE`, and a line that is not a JSON object maps to
  :attr:`AgentEventType.MALFORMED` — neither raises, so a single bad line never
  aborts the turn. Failing a turn on malformed output (``malformed_output``, §10.6)
  is the turn-lifecycle layer's decision, not the parser's.
* **No outcome, no envelope.** Mapping a ``result`` event to ``turn_completed`` vs
  ``turn_failed`` is event classification (§10.4). Corroborating that with the
  process exit code / timeouts to decide the *turn outcome*, capturing ``session_id``
  into worker state, and extracting tokens/cost are the turn-lifecycle layer's job
  (SPEC §10.2, §10.6). The runtime envelope a runner adds when emitting upstream
  (timestamp, ``agent_pid``, ``usage``, ``cost_usd``; §10.4) is likewise added
  there, reading from :attr:`AgentEvent.raw`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["AgentEventType", "AgentEvent", "parse_event", "parse_stream"]


class AgentEventType(StrEnum):
    """Normalized event names the parser classifies lines into (SPEC §10.4).

    Only the *line-derivable* events are produced here. Runner-derived events
    (``startup_failed``, ``turn_timed_out`` from timeouts; ``permission_denied``,
    ``rate_limited`` derived from a ``result``/``api_retry`` payload) are added by
    the turn-lifecycle layer.
    """

    SESSION_STARTED = "session_started"
    API_RETRY = "api_retry"
    TURN_COMPLETED = "turn_completed"
    TURN_FAILED = "turn_failed"
    NOTIFICATION = "notification"
    OTHER_MESSAGE = "other_message"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One normalized ``stream-json`` event (SPEC §10.4).

    Attributes:
        type: The normalized event classification.
        raw: The parsed CLI event object (``{}`` for a malformed line).
        line: The original stdout line, preserved for logging/diagnostics.
        session_id: ``session_id`` when the event carries one (``system``/``init``
            and ``result`` events).
        subtype: The CLI event ``subtype`` (for example ``init``, ``success``,
            ``error_max_turns``), when present.
        is_terminal: ``True`` for a ``result`` event — the turn boundary.
    """

    type: AgentEventType
    raw: dict[str, Any] = field(default_factory=dict)
    line: str = ""
    session_id: str | None = None
    subtype: str | None = None
    is_terminal: bool = False


def parse_event(line: str) -> AgentEvent:
    """Parse and classify one stdout line into an :class:`AgentEvent` (SPEC §10.4).

    Never raises: a line that is not a JSON object becomes a
    :attr:`AgentEventType.MALFORMED` event, and unrecognized event types become
    :attr:`AgentEventType.OTHER_MESSAGE` (SPEC §10.3 tolerance).
    """
    try:
        parsed = json.loads(line)
    except ValueError:
        return AgentEvent(AgentEventType.MALFORMED, line=line)
    if not isinstance(parsed, dict):
        # Valid JSON but not an event object (e.g. an array) — unusable as protocol.
        return AgentEvent(AgentEventType.MALFORMED, line=line)

    event_type = parsed.get("type")
    subtype = parsed.get("subtype")
    subtype = subtype if isinstance(subtype, str) else None
    session_id = parsed.get("session_id")
    session_id = session_id if isinstance(session_id, str) else None

    classified = _classify(event_type, subtype, parsed)
    return AgentEvent(
        type=classified,
        raw=parsed,
        line=line,
        session_id=session_id,
        subtype=subtype,
        is_terminal=event_type == "result",
    )


def _classify(
    event_type: Any, subtype: str | None, parsed: dict[str, Any]
) -> AgentEventType:
    """Map a CLI ``type``/``subtype`` to a normalized :class:`AgentEventType`."""
    if event_type == "system":
        if subtype == "init":
            return AgentEventType.SESSION_STARTED
        if subtype == "api_retry":
            return AgentEventType.API_RETRY
        return AgentEventType.OTHER_MESSAGE
    if event_type == "result":
        # Success requires both subtype "success" and a falsey is_error; any error_*
        # subtype or is_error true is a failed turn (SPEC §10.3).
        if subtype == "success" and not parsed.get("is_error"):
            return AgentEventType.TURN_COMPLETED
        return AgentEventType.TURN_FAILED
    if event_type == "assistant":
        return AgentEventType.NOTIFICATION
    # user / stream_event / unknown types: recognized-but-non-actionable, tolerated.
    return AgentEventType.OTHER_MESSAGE


def parse_stream(lines: Iterable[str]) -> Iterator[AgentEvent]:
    """Parse a sequence of stdout lines into events, skipping blank lines.

    Blank/whitespace-only lines (for example a trailing newline) are not protocol
    data and are skipped rather than reported as malformed.
    """
    for line in lines:
        if not line.strip():
            continue
        yield parse_event(line)
