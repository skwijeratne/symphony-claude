"""Single-turn lifecycle: drive one ``claude`` turn to an outcome (SPEC §10.2, §10.6).

Runs exactly one headless turn end to end: launch the subprocess (via
:func:`~symphony.agent_launcher.launch_turn`), read its ``stream-json`` stdout
through :func:`~symphony.stream_parser.parse_event`, enforce the startup and turn
timeouts, then map the terminal ``result`` event plus the process exit code to a
:class:`TurnResult` (outcome + captured ``session_id`` + token/cost accounting).

Timeouts owned here (SPEC §10.6):

* ``read_timeout_ms`` — the first ``system``/``init`` event must arrive within this
  window or startup is failed (``startup_timeout``).
* ``turn_timeout_ms`` — total wall-clock for the turn; on expiry the subprocess is
  killed (``turn_timeout``).

``stall_timeout_ms`` is intentionally *not* enforced here — SPEC §10.6 makes event
inactivity an orchestrator concern (reconciliation, §8.5).

Outcome (SPEC §10.3, §10.6): a turn succeeds only when the terminal ``result`` has
subtype ``success`` with a falsey ``is_error`` *and* the process exit code does not
contradict it. Failures are mapped to the typed agent-session errors and returned in
:attr:`TurnResult.error` — :func:`run_turn` does not raise for a failed turn, so the
worker loop (SPEC §16.5) can read the session id and accounting on success or
failure alike.

The multi-turn continuation loop, workspace/prompt assembly, and the Agent Runner
contract (SPEC §10.7, §16.5) build on this in a later PR.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from symphony.agent_launcher import launch_turn
from symphony.config import ClaudeConfig
from symphony.exceptions import (
    AgentApiError,
    AgentSessionError,
    MaxBudgetError,
    MaxTurnsError,
    ProcessExitError,
    StartupTimeoutError,
    TurnFailedError,
    TurnTimeoutError,
)
from symphony.stream_parser import AgentEvent, AgentEventType, parse_event

__all__ = ["TurnResult", "run_turn"]

_MS_PER_S = 1000

# Result subtypes that map to a dedicated error (SPEC §10.6); any other non-success
# subtype (or is_error true) maps to the generic turn_failed.
_SUBTYPE_SUCCESS = "success"
_SUBTYPE_MAX_TURNS = "error_max_turns"
_SUBTYPE_MAX_BUDGET = "error_max_budget_usd"

# Sentinel pushed on the line queue when a reader thread reaches EOF.
_EOF = object()

# Grace period to reap the subprocess after its terminal result before force-killing.
_EXIT_GRACE_S = 5.0
# Bytes of captured stderr to surface in a process-exit error message.
_STDERR_SNIPPET = 500


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome and accounting for one completed turn (SPEC §10.3-10.6, §4.1.6).

    Attributes:
        error: The mapped failure, or ``None`` when the turn succeeded.
        session_id: The session id (passed in, or captured from ``init``/``result``).
        result_event: The terminal ``result`` event, when one was received.
        exit_code: The subprocess exit code, when the process was reaped.
        input_tokens / output_tokens / total_tokens: Token usage from the ``result``
            event (0 when absent).
        cost_usd: ``total_cost_usd`` from the ``result`` event, when present.
        num_turns: ``num_turns`` reported by the ``result`` event, when present.
    """

    error: AgentSessionError | None
    session_id: str | None
    result_event: AgentEvent | None
    exit_code: int | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    num_turns: int | None

    @property
    def succeeded(self) -> bool:
        """Whether the turn completed successfully."""
        return self.error is None


def run_turn(
    config: ClaudeConfig,
    *,
    workspace_path: Path,
    prompt: str,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    on_event: Callable[[AgentEvent], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TurnResult:
    """Run one headless turn and return its outcome (SPEC §10.2-10.6).

    Args:
        config: The resolved ``claude`` config (command, flags, timeouts).
        workspace_path: The per-issue workspace; the subprocess ``cwd``.
        prompt: The rendered turn prompt.
        session_id: A fixed session id for a first turn (``--session-id``).
        resume_session_id: The session id to continue (``--resume``).
        on_event: Optional callback invoked with every parsed event as it arrives
            (the runner's upstream forwarding, SPEC §10.7 step 4).
        clock: Monotonic clock source; injectable for tests.

    Returns:
        The :class:`TurnResult`; a failed turn carries the mapped error in
        :attr:`TurnResult.error` rather than raising.
    """
    try:
        process = launch_turn(
            config,
            workspace_path=workspace_path,
            prompt=prompt,
            session_id=session_id,
            resume_session_id=resume_session_id,
        )
    except AgentSessionError as exc:
        return _failed(exc, session_id=resume_session_id or session_id)

    drive = _drive_turn(
        process,
        read_timeout_s=config.read_timeout_ms / _MS_PER_S,
        turn_timeout_s=config.turn_timeout_ms / _MS_PER_S,
        on_event=on_event,
        clock=clock,
    )

    known_session = resume_session_id or session_id or drive.session_id
    accounting = _extract_accounting(drive.result_event)
    return TurnResult(
        error=_classify(drive),
        session_id=known_session,
        result_event=drive.result_event,
        exit_code=drive.exit_code,
        input_tokens=accounting.input_tokens,
        output_tokens=accounting.output_tokens,
        total_tokens=accounting.total_tokens,
        cost_usd=accounting.cost_usd,
        num_turns=accounting.num_turns,
    )


@dataclass(slots=True)
class _DriveResult:
    """Raw observations from draining one turn's subprocess."""

    result_event: AgentEvent | None
    session_id: str | None
    exit_code: int | None
    startup_timed_out: bool
    turn_timed_out: bool
    stderr_tail: str


def _drive_turn(
    process: subprocess.Popen[str],
    *,
    read_timeout_s: float,
    turn_timeout_s: float,
    on_event: Callable[[AgentEvent], None] | None,
    clock: Callable[[], float],
) -> _DriveResult:
    """Read stdout to the terminal result/EOF, enforcing the §10.6 timeouts."""
    # stdout/stderr are always pipes here (launch_turn sets stdin/out/err=PIPE).
    if process.stdout is None:  # pragma: no cover - defensive
        raise ProcessExitError("subprocess stdout pipe is unavailable")

    lines: queue.Queue[object] = queue.Queue()
    reader = threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True)
    reader.start()
    stderr = process.stderr
    stderr_lines: list[str] = []
    stderr_reader = _drain_stderr(stderr, stderr_lines)

    start = clock()
    turn_deadline = start + turn_timeout_s
    init_deadline = start + read_timeout_s
    session_id: str | None = None
    result_event: AgentEvent | None = None
    started = False
    startup_timed_out = False
    turn_timed_out = False

    while True:
        now = clock()
        if now >= turn_deadline:
            turn_timed_out = True
            break
        wait = turn_deadline - now
        if not started:
            if now >= init_deadline:
                startup_timed_out = True
                break
            wait = min(wait, init_deadline - now)
        try:
            item = lines.get(timeout=max(0.0, wait))
        except queue.Empty:
            continue
        if item is _EOF:
            break
        event = parse_event(str(item))
        if on_event is not None:
            on_event(event)
        if event.session_id and session_id is None:
            session_id = event.session_id
        if event.type is AgentEventType.SESSION_STARTED:
            started = True
        if event.is_terminal:
            result_event = event
            break

    exit_code = _finish(process, killed=startup_timed_out or turn_timed_out)
    reader.join(timeout=1.0)
    if stderr_reader is not None:
        stderr_reader.join(timeout=1.0)
    return _DriveResult(
        result_event=result_event,
        session_id=session_id,
        exit_code=exit_code,
        startup_timed_out=startup_timed_out,
        turn_timed_out=turn_timed_out,
        stderr_tail="".join(stderr_lines)[-_STDERR_SNIPPET:].strip(),
    )


def _pump(stream: IO[str], lines: queue.Queue[object]) -> None:
    """Forward each stdout line onto the queue, then signal EOF."""
    try:
        for line in stream:
            lines.put(line)
    finally:
        lines.put(_EOF)


def _drain_stderr(stream: IO[str] | None, sink: list[str]) -> threading.Thread | None:
    """Continuously drain stderr so a full stderr pipe can't deadlock the turn."""
    if stream is None:  # pragma: no cover - stderr=PIPE always sets this
        return None

    def drain() -> None:
        for line in stream:
            sink.append(line)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return thread


def _finish(process: subprocess.Popen[str], *, killed: bool) -> int | None:
    """Reap the subprocess, killing it on timeout or if it overstays the grace."""
    if killed:
        process.kill()
        process.wait()
        return None
    try:
        return process.wait(timeout=_EXIT_GRACE_S)
    except subprocess.TimeoutExpired:  # lingering after its terminal result
        process.kill()
        process.wait()
        return None


def _classify(drive: _DriveResult) -> AgentSessionError | None:
    """Map drained observations to a turn outcome (SPEC §10.3, §10.6)."""
    if drive.startup_timed_out:
        return StartupTimeoutError("no init event within read_timeout_ms")
    if drive.turn_timed_out:
        return TurnTimeoutError("turn exceeded turn_timeout_ms")

    result = drive.result_event
    if result is None:
        detail = f" stderr: {drive.stderr_tail}" if drive.stderr_tail else ""
        return ProcessExitError(
            f"subprocess exited (code {drive.exit_code}) with no result event.{detail}"
        )

    subtype = result.subtype
    is_error = bool(result.raw.get("is_error"))
    if subtype == _SUBTYPE_SUCCESS and not is_error:
        # Corroborate the success result with the exit code (SPEC §10.3).
        if drive.exit_code not in (0, None):
            return ProcessExitError(
                f"result reported success but process exited {drive.exit_code}"
            )
        return None

    if subtype == _SUBTYPE_MAX_TURNS:
        return MaxTurnsError("turn ended: error_max_turns")
    if subtype == _SUBTYPE_MAX_BUDGET:
        return MaxBudgetError("turn ended: error_max_budget_usd")
    if result.raw.get("api_error_status"):
        return AgentApiError(
            f"terminating API error: {result.raw.get('api_error_status')}"
        )
    return TurnFailedError(f"turn failed: subtype={subtype} is_error={is_error}")


@dataclass(frozen=True, slots=True)
class _Accounting:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    num_turns: int | None


def _extract_accounting(result_event: AgentEvent | None) -> _Accounting:
    """Pull token usage and cost from the terminal ``result`` event (SPEC §4.1.6)."""
    raw = result_event.raw if result_event is not None else {}
    usage = raw.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    total = usage.get("total_tokens")
    total_tokens = _as_int(total) if total is not None else input_tokens + output_tokens
    num_turns_raw = raw.get("num_turns")
    return _Accounting(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=_as_float(raw.get("total_cost_usd")),
        num_turns=_as_int(num_turns_raw) if num_turns_raw is not None else None,
    )


def _failed(error: AgentSessionError, *, session_id: str | None) -> TurnResult:
    """A :class:`TurnResult` for a failure that produced no subprocess output."""
    return TurnResult(
        error=error,
        session_id=session_id,
        result_event=None,
        exit_code=None,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_usd=None,
        num_turns=None,
    )


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None
