"""Tests for structured logging conventions and sinks (SPEC §13.1-13.2, §17.6)."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from symphony.structured_logging import (
    SYMPHONY_LOGGER_NAME,
    ResilientHandler,
    configure_logging,
    log_fields,
)


@pytest.fixture(autouse=True)
def _restore_symphony_logger() -> Iterator[None]:
    """Undo ``configure_logging``'s global handler/level changes after each test."""
    root = logging.getLogger(SYMPHONY_LOGGER_NAME)
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


class _FlakySink(logging.Handler):
    """A sink that raises while ``fail`` is set and records messages otherwise."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if self.fail:
            raise OSError("disk full")
        self.messages.append(record.getMessage())


# --- log_fields (SPEC §13.1) ----------------------------------------------------
def test_log_fields_renders_stable_key_value_pairs() -> None:
    rendered = log_fields(issue_id="i-1", issue_identifier="ABC-1", attempt=2)
    assert rendered == "issue_id=i-1 issue_identifier=ABC-1 attempt=2"


def test_log_fields_omits_none_values() -> None:
    # Optional context (e.g. ``session_id`` before a session exists) can be
    # passed unconditionally without producing ``key=None`` noise.
    assert log_fields(issue_id="i-1", session_id=None) == "issue_id=i-1"


def test_log_fields_quotes_values_that_break_the_phrasing() -> None:
    rendered = log_fields(
        reason="failed to spawn agent",
        expr="a=b",
        quoted='say "hi"',
        empty="",
    )
    assert rendered == (
        'reason="failed to spawn agent" expr="a=b" quoted="say \\"hi\\"" empty=""'
    )


# --- ResilientHandler (SPEC §13.2) ----------------------------------------------
def test_failing_sink_does_not_raise_and_warns_through_remaining_sink() -> None:
    broken = _FlakySink()
    broken.fail = True
    healthy = _FlakySink()
    logger = configure_logging(extra_handlers=(broken, healthy))

    logger.info("dispatch completed issue_id=i-1")  # must not raise

    assert broken.messages == []
    assert "dispatch completed issue_id=i-1" in healthy.messages
    warnings = [m for m in healthy.messages if "log sink failed" in m]
    assert len(warnings) == 1
    assert "sink=_FlakySink" in warnings[0]


def test_persistently_failing_sink_warns_only_once() -> None:
    broken = _FlakySink()
    broken.fail = True
    healthy = _FlakySink()
    logger = configure_logging(extra_handlers=(broken, healthy))

    logger.info("first")
    logger.info("second")

    warnings = [m for m in healthy.messages if "log sink failed" in m]
    assert len(warnings) == 1


def test_recovered_sink_resumes_and_warns_again_on_a_new_failure() -> None:
    flaky = _FlakySink()
    healthy = _FlakySink()
    logger = configure_logging(extra_handlers=(flaky, healthy))

    flaky.fail = True
    logger.info("one")
    flaky.fail = False
    logger.info("two")  # sink recovered: records flow again
    flaky.fail = True
    logger.info("three")

    assert "two" in flaky.messages
    warnings = [m for m in healthy.messages if "log sink failed" in m]
    assert len(warnings) == 2


def test_sole_failing_sink_still_does_not_crash() -> None:
    broken = _FlakySink()
    broken.fail = True
    root = logging.getLogger(SYMPHONY_LOGGER_NAME)
    root.handlers[:] = [ResilientHandler(broken)]

    logging.getLogger("symphony.orchestrator").info("anything")  # must not raise


def test_resilient_handler_close_closes_the_wrapped_sink() -> None:
    closed: list[bool] = []

    class _Sink(logging.Handler):
        def close(self) -> None:
            closed.append(True)
            super().close()

    ResilientHandler(_Sink()).close()
    assert closed == [True]


# --- configure_logging (SPEC §13.2, §16.1) ---------------------------------------
def test_configure_logging_writes_to_the_stream_with_context() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)

    logging.getLogger("symphony.orchestrator").info(
        "dispatch completed %s", log_fields(issue_id="i-1", issue_identifier="ABC-1")
    )

    line = stream.getvalue()
    assert "INFO symphony.orchestrator" in line
    assert "issue_id=i-1 issue_identifier=ABC-1" in line


def test_configure_logging_replaces_handlers_from_a_previous_call() -> None:
    first = io.StringIO()
    second = io.StringIO()
    configure_logging(stream=first)
    logger = configure_logging(stream=second)

    logger.info("only once")

    assert first.getvalue() == ""
    assert second.getvalue().count("only once") == 1


def test_configure_logging_wraps_every_sink_resiliently() -> None:
    logger = configure_logging(extra_handlers=(_FlakySink(),))
    assert all(isinstance(h, ResilientHandler) for h in logger.handlers)
    assert len(logger.handlers) == 2  # default stream sink + the extra
