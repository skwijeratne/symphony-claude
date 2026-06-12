"""Structured logging conventions and resilient sinks (SPEC §13.1-13.2).

Symphony logs through the stdlib ``logging`` package under the ``symphony.*``
logger namespace. This module supplies the two pieces the SPEC requires on top
of that:

- :func:`log_fields` renders context fields in the stable ``key=value`` phrasing
  REQUIRED by SPEC §13.1, so issue-related logs can carry ``issue_id`` /
  ``issue_identifier`` and session lifecycle logs can carry ``session_id`` in a
  uniform, grep-able form.
- :class:`ResilientHandler` wraps a concrete sink so a sink failure never takes
  down orchestration: the record is dropped for that sink only and an
  operator-visible warning is emitted through the remaining sinks (SPEC §13.2).

:func:`configure_logging` is the host-startup entry point (SPEC §16.1
``configure_logging()``): it installs resilient handlers on the ``symphony``
root logger so operators can see startup/validation/dispatch failures without
attaching a debugger (SPEC §13.2).
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

__all__ = [
    "SYMPHONY_LOGGER_NAME",
    "log_fields",
    "ResilientHandler",
    "configure_logging",
]

SYMPHONY_LOGGER_NAME = "symphony"
"""Root logger name every ``symphony.<module>`` logger propagates to."""

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

logger = logging.getLogger("symphony.structured_logging")


def _format_value(value: object) -> str:
    """Render one field value for ``key=value`` output (SPEC §13.1).

    Values that would break the phrasing's grep-ability (whitespace, ``=``,
    quotes, or an empty string) are double-quoted with backslash escaping; all
    other values pass through ``str()`` unquoted.
    """
    text = str(value)
    if text and not any(ch.isspace() or ch in '="' for ch in text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def log_fields(**fields: object) -> str:
    """Format context fields in the stable ``key=value`` phrasing (SPEC §13.1).

    ``None`` values are omitted, so call sites can pass optional context (for
    example ``session_id`` before a session exists) unconditionally:

        logger.info("dispatch completed %s", log_fields(issue_id=..., ...))

    Returns:
        The space-separated ``key=value`` pairs, in keyword order.
    """
    return " ".join(
        f"{key}={_format_value(value)}"
        for key, value in fields.items()
        if value is not None
    )


class ResilientHandler(logging.Handler):
    """A handler wrapper whose sink failures never crash orchestration.

    SPEC §13.2: if a configured log sink fails, the service SHOULD continue
    running when possible and emit an operator-visible warning through any
    remaining sink. ``emit`` therefore catches every exception from the wrapped
    handler and, on the transition from healthy to failed, logs one warning —
    which the logging tree fans out to the *other* configured handlers. The
    failed sink keeps being attempted, so a transient failure (full disk,
    rotated file) recovers on its own; the healthy/failed flag only gates the
    warning so a persistently broken sink does not warn once per record.
    """

    def __init__(self, target: logging.Handler, *, sink_name: str | None = None):
        """Wrap ``target``.

        Args:
            target: The concrete handler doing the writing.
            sink_name: Name used in the sink-failure warning; defaults to the
                target's class name.
        """
        super().__init__()
        self._target = target
        self._sink_name = sink_name or type(target).__name__
        self._failed = False

    def emit(self, record: logging.LogRecord) -> None:
        """Forward ``record`` to the wrapped sink, absorbing its failures."""
        try:
            self._target.emit(record)
        except Exception as exc:  # any sink failure must be absorbed (SPEC §13.2)
            newly_failed = not self._failed
            self._failed = True
            if newly_failed:
                logger.warning(
                    "log sink failed; continuing without it %s",
                    log_fields(sink=self._sink_name, error=exc),
                )
        else:
            self._failed = False

    def close(self) -> None:
        """Close the wrapped sink along with this handler."""
        try:
            self._target.close()
        finally:
            super().close()


def configure_logging(
    *,
    level: int = logging.INFO,
    stream: TextIO | None = None,
    extra_handlers: tuple[logging.Handler, ...] = (),
) -> logging.Logger:
    """Install resilient sinks on the ``symphony`` root logger (SPEC §16.1).

    Replaces any handlers from a previous call, so the function is idempotent
    across restarts/tests. The default sink is a stream handler on ``stderr``,
    satisfying SPEC §13.2's requirement that startup/validation/dispatch
    failures are operator-visible; additional sinks MAY be supplied and each is
    wrapped in a :class:`ResilientHandler` independently so one failing sink
    cannot silence the others.

    Args:
        level: Threshold for the ``symphony`` logger.
        stream: Stream for the default sink; defaults to ``sys.stderr``.
        extra_handlers: Additional concrete sinks to install, each wrapped.

    Returns:
        The configured ``symphony`` root logger.
    """
    root = logging.getLogger(SYMPHONY_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT)
    stream_handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    for target in (stream_handler, *extra_handlers):
        target.setFormatter(formatter)
        root.addHandler(ResilientHandler(target, sink_name=type(target).__name__))

    root.setLevel(level)
    return root
