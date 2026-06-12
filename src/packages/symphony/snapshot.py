"""Runtime snapshot / monitoring interface (SPEC §13.3).

:func:`build_snapshot` is a pure projection of the authoritative
:class:`~symphony.models.OrchestratorState` into an immutable
:class:`RuntimeSnapshot` carrying the SPEC §13.3 shape: ``running`` rows (with
``turn_count`` and the tracker issue URL when available), ``retrying`` rows,
``agent_totals`` (including live ``seconds_running``), and the latest
``rate_limits``. Aggregate runtime follows SPEC §13.5: the cumulative
ended-session counter plus each active session's elapsed time, computed at
snapshot time rather than ticked in the background.

:class:`SnapshotProvider` is the synchronous request side for dashboards and
monitoring. How a consumer thread obtains the orchestrator state is
runtime-specific (M7 wires it to the event loop), so it is an injected seam;
the provider maps the seam's outcomes onto the SPEC §13.3 error modes —
``timeout`` (:class:`~symphony.exceptions.SnapshotTimeoutError`) and
``unavailable`` (:class:`~symphony.exceptions.SnapshotUnavailableError`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from symphony.exceptions import SnapshotTimeoutError, SnapshotUnavailableError
from symphony.models import AgentRateLimits, OrchestratorState

__all__ = [
    "RunningRow",
    "RetryRow",
    "SnapshotTotals",
    "RuntimeSnapshot",
    "build_snapshot",
    "StateRequester",
    "SnapshotProvider",
]


def _utcnow() -> datetime:
    """Return the current UTC time (injectable seam for deterministic tests)."""
    return datetime.now(tz=UTC)


def _monotonic_ms() -> int:
    """Monotonic clock in ms, matching retry ``due_at_ms`` (SPEC §4.1.7)."""
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class RunningRow:
    """One running session in the snapshot (SPEC §13.3 ``running``).

    Attributes:
        issue_id: Stable tracker-internal ID of the issue.
        issue_identifier: Human-readable ticket key.
        issue_url: Tracker URL for the issue, when available.
        attempt: ``None`` for a first run; ``>=1`` for retries/continuation.
        started_at: When the attempt started.
        seconds_running: Elapsed seconds for this session as of snapshot time.
        session_id: Claude Code session ID, once a session has started.
        turn_count: Coding-agent turns started in this worker run (SPEC §13.3).
        total_tokens: Aggregate tokens across this run's turns so far.
    """

    issue_id: str
    issue_identifier: str
    issue_url: str | None
    attempt: int | None
    started_at: datetime
    seconds_running: float
    session_id: str | None
    turn_count: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class RetryRow:
    """One retry-queue entry in the snapshot (SPEC §13.3 ``retrying``).

    Attributes:
        issue_id: Stable tracker-internal ID of the issue.
        issue_identifier: Best-effort human ID (SPEC §4.1.7).
        attempt: 1-based retry counter.
        due_in_ms: Milliseconds until the retry fires, floored at ``0``. The
            entry's raw ``due_at_ms`` is monotonic-clock and meaningless outside
            the process, so the snapshot exposes the relative deadline instead.
        error: The error that queued the retry, if any.
    """

    issue_id: str
    issue_identifier: str
    attempt: int
    due_in_ms: int
    error: str | None


@dataclass(frozen=True, slots=True)
class SnapshotTotals:
    """Aggregate agent accounting in the snapshot (SPEC §13.3 ``agent_totals``).

    Attributes:
        input_tokens: Total input tokens across all runs.
        output_tokens: Total output tokens across all runs.
        total_tokens: Total tokens across all runs.
        cost_usd: Aggregate reported cost as of snapshot time.
        seconds_running: Aggregate runtime seconds as of snapshot time,
            including active sessions (SPEC §13.5).
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    seconds_running: float


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """A point-in-time view of the orchestrator (SPEC §13.3).

    Attributes:
        running: One row per live worker session.
        retrying: One row per scheduled retry.
        agent_totals: Aggregate tokens, cost, and runtime.
        rate_limits: Latest rate-limit / API-retry snapshot.
        taken_at: When the snapshot was built.
    """

    running: tuple[RunningRow, ...]
    retrying: tuple[RetryRow, ...]
    agent_totals: SnapshotTotals
    rate_limits: AgentRateLimits
    taken_at: datetime


def build_snapshot(
    state: OrchestratorState,
    *,
    now: Callable[[], datetime] = _utcnow,
    now_ms: Callable[[], int] = _monotonic_ms,
) -> RuntimeSnapshot:
    """Project ``state`` into an immutable :class:`RuntimeSnapshot` (SPEC §13.3).

    Pure read: the state is not mutated and the snapshot shares no mutable
    structure with it, so a consumer can hold the snapshot while the
    orchestrator keeps mutating its state.

    Args:
        state: The authoritative orchestrator state to project.
        now: Wall clock for ``taken_at`` and elapsed-time math (test seam).
        now_ms: Monotonic clock matching retry ``due_at_ms`` (test seam).

    Returns:
        The snapshot, with rows ordered by issue identifier for stable output.
    """
    taken_at = now()
    current_ms = now_ms()

    running_rows = []
    active_seconds = 0.0
    for entry in state.running.values():
        attempt = entry.run_attempt
        elapsed = max((taken_at - attempt.started_at).total_seconds(), 0.0)
        active_seconds += elapsed
        session = entry.session
        running_rows.append(
            RunningRow(
                issue_id=attempt.issue_id,
                issue_identifier=attempt.issue_identifier,
                issue_url=entry.issue.url if entry.issue is not None else None,
                attempt=attempt.attempt,
                started_at=attempt.started_at,
                seconds_running=elapsed,
                session_id=session.session_id if session is not None else None,
                turn_count=session.turn_count if session is not None else 0,
                total_tokens=session.total_tokens if session is not None else 0,
            )
        )

    retry_rows = [
        RetryRow(
            issue_id=retry.issue_id,
            issue_identifier=retry.identifier,
            attempt=retry.attempt,
            due_in_ms=max(retry.due_at_ms - current_ms, 0),
            error=retry.error,
        )
        for retry in state.retry_attempts.values()
    ]

    totals = state.agent_totals
    return RuntimeSnapshot(
        running=tuple(sorted(running_rows, key=lambda r: r.issue_identifier)),
        retrying=tuple(sorted(retry_rows, key=lambda r: r.issue_identifier)),
        agent_totals=SnapshotTotals(
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            total_tokens=totals.total_tokens,
            cost_usd=totals.total_cost_usd,
            seconds_running=totals.runtime_seconds + active_seconds,
        ),
        rate_limits=replace(state.agent_rate_limits),
        taken_at=taken_at,
    )


# Obtains the orchestrator state for a snapshot, waiting at most ``timeout_s``
# seconds. Returns ``None`` when the orchestrator is not running, and raises
# ``TimeoutError`` when the state could not be obtained in time. The concrete
# mechanism (event-loop call, queue round-trip, …) is wired in M7.
StateRequester = Callable[[float], OrchestratorState | None]


@dataclass(frozen=True, slots=True)
class SnapshotProvider:
    """Synchronous snapshot interface for monitoring consumers (SPEC §13.3).

    Maps the :data:`StateRequester` seam's outcomes onto the SPEC §13.3 error
    modes so every consumer sees the same typed surface regardless of how the
    runtime hands over state.

    Attributes:
        request_state: Seam that obtains the orchestrator state.
        timeout_ms: Budget for one snapshot request.
        now: Wall clock passed through to :func:`build_snapshot` (test seam).
        now_ms: Monotonic clock passed through to :func:`build_snapshot`.
    """

    request_state: StateRequester
    timeout_ms: int = 2000
    now: Callable[[], datetime] = _utcnow
    now_ms: Callable[[], int] = _monotonic_ms

    def take(self) -> RuntimeSnapshot:
        """Request the state and build a snapshot from it.

        Returns:
            The current :class:`RuntimeSnapshot`.

        Raises:
            SnapshotTimeoutError: The state was not obtained within
                ``timeout_ms`` (SPEC §13.3 ``timeout``).
            SnapshotUnavailableError: The orchestrator is not running
                (SPEC §13.3 ``unavailable``).
        """
        try:
            state = self.request_state(self.timeout_ms / 1000.0)
        except TimeoutError as exc:
            raise SnapshotTimeoutError(
                f"snapshot request exceeded {self.timeout_ms} ms"
            ) from exc
        if state is None:
            raise SnapshotUnavailableError("orchestrator state is not available")
        return build_snapshot(state, now=self.now, now_ms=self.now_ms)
