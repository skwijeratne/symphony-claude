"""Orchestrator state primitives and single-issue dispatch (SPEC §7, §16.4).

The orchestrator is the only component that mutates scheduling state; all worker
outcomes are reported back to it and converted into explicit state transitions
(SPEC §7). This module provides:

- the claim / running bookkeeping primitives that guard against duplicate
  dispatch (SPEC §7.1, §7.4), and
- ``dispatch_issue`` (SPEC §16.4), which spawns a worker for one issue and records
  the resulting claim/running state.

Candidate selection and sorting (§8.2-8.3), retry/backoff (§8.4, §16.6),
reconciliation (§8.5, §16.3) and the full poll tick (§8.1, §16.2) land in later
PRs; this module exposes the seams they plug into (``WorkerSpawner`` and
``ScheduleRetry``).

State mutations are applied in place on the single authoritative
:class:`~symphony.models.OrchestratorState` and the same instance is returned, so
callers can chain steps in the functional style of the SPEC §16 reference
algorithms.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from symphony.config import ServiceConfig
from symphony.exceptions import TrackerError
from symphony.models import (
    Issue,
    OrchestratorState,
    RetryEntry,
    RunAttempt,
    RunAttemptPhase,
    RunningEntry,
)
from symphony.normalization import normalize_label, normalize_state
from symphony.workspace import workspace_path_for

__all__ = [
    "WorkerHandle",
    "WorkerSpawner",
    "ScheduleRetry",
    "DispatchPolicy",
    "RetryDelay",
    "SetRetryTimer",
    "CancelRetryTimer",
    "CandidateFetcher",
    "DispatchFn",
    "RetryScheduler",
    "next_attempt",
    "is_running",
    "is_claimed",
    "claim",
    "release",
    "mark_completed",
    "available_slots",
    "running_count_for_state",
    "per_state_available_slots",
    "should_dispatch",
    "sort_for_dispatch",
    "dispatch_issue",
    "compute_backoff_ms",
    "on_retry_timer",
]

# Opaque runtime handle for a spawned worker. The concrete task/process type is
# decided when the service event loop is composed (M7); dispatch only stores it.
WorkerHandle = object


class WorkerSpawner(Protocol):
    """Spawns the worker that runs an attempt for one issue (SPEC §16.4).

    The worker executes ``run_agent_attempt`` (SPEC §16.5) on the configured
    concurrency runtime. This seam returns a handle on success, or ``None`` when
    the worker could not be spawned, so :func:`dispatch_issue` can route the
    failure to a retry without depending on the runtime details.
    """

    def __call__(self, issue: Issue, attempt: int | None) -> WorkerHandle | None: ...


class ScheduleRetry(Protocol):
    """Schedules a retry for an issue and returns the updated state (SPEC §16.6).

    Backoff timing, timer handling and slot-exhaustion requeue are defined in
    SPEC §8.4 and implemented in a later PR; :func:`dispatch_issue` depends only on
    this minimal contract for its spawn-failure path.
    """

    def __call__(
        self,
        state: OrchestratorState,
        issue_id: str,
        attempt: int,
        *,
        identifier: str,
        error: str | None,
    ) -> OrchestratorState: ...


@dataclass(frozen=True, slots=True)
class DispatchPolicy:
    """Normalized candidate-selection criteria derived from config (SPEC §8.2-8.3).

    The active/terminal state sets and required-label set depend only on
    ``tracker`` config, which changes only on reload (SPEC §6.2). Building this
    once per dispatch pass (via :meth:`from_config`) avoids re-normalizing the same
    config for every candidate that :func:`should_dispatch` examines.

    Attributes:
        active_states: Normalized states an issue must be in to dispatch.
        terminal_states: Normalized states that exclude an issue and that a
            ``Todo`` issue's blockers must be in.
        required_labels: Normalized labels an issue must all carry.
        max_concurrent_by_state: Per-state concurrency overrides (keys already
            normalized by the config layer); empty means "use the global limit".
    """

    active_states: frozenset[str] = field(default_factory=frozenset)
    terminal_states: frozenset[str] = field(default_factory=frozenset)
    required_labels: frozenset[str] = field(default_factory=frozenset)
    max_concurrent_by_state: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: ServiceConfig) -> DispatchPolicy:
        """Precompute the normalized selection criteria from ``config``."""
        return cls(
            active_states=frozenset(
                normalize_state(s) for s in config.tracker.active_states
            ),
            terminal_states=frozenset(
                normalize_state(s) for s in config.tracker.terminal_states
            ),
            # Match the trim+lowercase the tracker layer applies to issue labels,
            # so required labels compare correctly (``normalize_label`` only lowers).
            required_labels=frozenset(
                normalize_label(label.strip())
                for label in config.tracker.required_labels
            ),
            max_concurrent_by_state=dict(config.agent.max_concurrent_agents_by_state),
        )


def _utcnow() -> datetime:
    """Return the current UTC time (injectable seam for deterministic tests)."""
    return datetime.now(tz=UTC)


def next_attempt(attempt: int | None) -> int:
    """Return the next retry attempt number (SPEC §16.4, §16.6).

    The first dispatch (``attempt=None``) escalates to attempt ``1``; an existing
    attempt ``N`` escalates to ``N + 1``.
    """
    if attempt is None:
        return 1
    return attempt + 1


def is_running(state: OrchestratorState, issue_id: str) -> bool:
    """Whether a worker is currently tracked for ``issue_id`` (SPEC §7.1)."""
    return issue_id in state.running


def is_claimed(state: OrchestratorState, issue_id: str) -> bool:
    """Whether ``issue_id`` is claimed against duplicate dispatch (SPEC §7.1)."""
    return issue_id in state.claimed


def claim(state: OrchestratorState, issue_id: str) -> OrchestratorState:
    """Reserve ``issue_id`` to prevent duplicate dispatch (SPEC §7.1, §7.4)."""
    state.claimed.add(issue_id)
    return state


def release(state: OrchestratorState, issue_id: str) -> OrchestratorState:
    """Drop the claim for ``issue_id`` (SPEC §7.1, ``Released``).

    Idempotent: releasing an unclaimed issue is a no-op.
    """
    state.claimed.discard(issue_id)
    return state


def mark_completed(state: OrchestratorState, issue_id: str) -> OrchestratorState:
    """Record ``issue_id`` as completed (bookkeeping only; SPEC §16.1, §16.6).

    ``completed`` does not gate dispatch — a successful worker exit does not mean
    the issue is done forever (SPEC §7.1).
    """
    state.completed.add(issue_id)
    return state


def available_slots(state: OrchestratorState) -> int:
    """Global concurrency slots still free (SPEC §8.3).

    ``max(max_concurrent_agents - running_count, 0)``.
    """
    return max(state.max_concurrent_agents - len(state.running), 0)


def running_count_for_state(state: OrchestratorState, state_name: str) -> int:
    """Number of running workers whose issue is in ``state_name`` (SPEC §8.3).

    Issues are counted by their current tracked state in the ``running`` map; a
    running entry whose issue is not yet known is not counted toward any state.
    """
    target = normalize_state(state_name)
    return sum(
        1
        for entry in state.running.values()
        if entry.issue is not None and entry.issue.normalized_state == target
    )


def per_state_available_slots(
    state: OrchestratorState, policy: DispatchPolicy, state_name: str
) -> int:
    """Per-state concurrency slots still free for ``state_name`` (SPEC §8.3).

    Uses the policy's per-state override when present, otherwise falls back to the
    global limit.
    """
    target = normalize_state(state_name)
    limit = policy.max_concurrent_by_state.get(target, state.max_concurrent_agents)
    return max(limit - running_count_for_state(state, target), 0)


def _has_core_fields(issue: Issue) -> bool:
    """Whether the issue carries the REQUIRED identity fields (SPEC §8.2)."""
    return bool(issue.id and issue.identifier and issue.title and issue.state)


def _todo_blockers_clear(issue: Issue, terminal_states: frozenset[str]) -> bool:
    """Todo blocker rule: every blocker must be terminal (SPEC §8.2).

    A blocker with an unknown state is treated as non-terminal (it blocks), since
    the orchestrator cannot confirm it is resolved.
    """
    return all(
        blocker.state is not None and normalize_state(blocker.state) in terminal_states
        for blocker in issue.blocked_by
    )


def should_dispatch(
    issue: Issue, state: OrchestratorState, policy: DispatchPolicy
) -> bool:
    """Whether ``issue`` is dispatch-eligible right now (SPEC §8.2).

    Checks identity, active/terminal state, required labels, the not-running and
    not-claimed claim guards (SPEC §7.1), global and per-state concurrency slots,
    and the ``Todo`` blocker rule. ``policy`` carries the normalized criteria
    precomputed once per pass (:meth:`DispatchPolicy.from_config`), so this stays
    allocation-free across candidates.

    Assignee routing (SPEC §8.2) is intentionally not applied: the normative config
    schema (SPEC §6.4) defines no assignee key and the ``Issue`` model (SPEC §4.1.1)
    carries no assignee, so with no configured assignee every candidate routes here.
    """
    if not _has_core_fields(issue):
        return False

    issue_state = issue.normalized_state
    if issue_state not in policy.active_states or issue_state in policy.terminal_states:
        return False

    if not policy.required_labels.issubset(issue.labels):
        return False

    if is_running(state, issue.id) or is_claimed(state, issue.id):
        return False

    if available_slots(state) <= 0:
        return False
    if per_state_available_slots(state, policy, issue.state) <= 0:
        return False

    # The blocker rule only applies to Todo issues (SPEC §8.2).
    return issue_state != "todo" or _todo_blockers_clear(issue, policy.terminal_states)


def _dispatch_sort_key(issue: Issue) -> tuple[bool, int, bool, datetime, str]:
    """Sort key implementing the dispatch order (SPEC §8.2).

    ``priority`` ascending (null last), then ``created_at`` oldest first (null
    last), then ``identifier`` lexicographically. The boolean null-flags guard the
    value slots so the ``datetime.min`` placeholder is only ever compared against
    itself (never against a real, possibly tz-aware timestamp).
    """
    return (
        issue.priority is None,
        issue.priority if issue.priority is not None else 0,
        issue.created_at is None,
        issue.created_at if issue.created_at is not None else datetime.min,
        issue.identifier,
    )


def sort_for_dispatch(issues: Iterable[Issue]) -> list[Issue]:
    """Return ``issues`` ordered by dispatch priority (SPEC §8.2).

    ``sorted`` is stable, so issues that tie on every key keep their input order.
    """
    return sorted(issues, key=_dispatch_sort_key)


def dispatch_issue(
    issue: Issue,
    state: OrchestratorState,
    attempt: int | None,
    *,
    workspace_root: Path,
    spawn_worker: WorkerSpawner,
    schedule_retry: ScheduleRetry,
    now: Callable[[], datetime] = _utcnow,
) -> OrchestratorState:
    """Spawn a worker for ``issue`` and record the claim/running state (SPEC §16.4).

    Eligibility (running/claimed/slot checks, SPEC §8.2-8.3) is the caller's
    responsibility; this function performs the dispatch itself. On a spawn failure
    it routes to ``schedule_retry`` with the escalated attempt; on success it adds
    the running entry, claims the issue, and clears any pending retry.

    The running entry records the issue's deterministic workspace path
    (``workspace_path_for``); the worker itself creates and validates that path
    when it starts (SPEC §9).

    Args:
        issue: The issue to dispatch.
        state: The authoritative orchestrator state (mutated in place).
        attempt: ``None`` for a first dispatch, ``>=1`` for a retry/continuation.
        workspace_root: Configured workspace root, used to derive the deterministic
            workspace path for the attempt.
        spawn_worker: Seam that spawns the worker (SPEC §16.5) and returns its
            handle, or ``None`` on failure.
        schedule_retry: Seam invoked when the worker could not be spawned.
        now: Clock seam for the attempt's ``started_at`` (injectable for tests).

    Returns:
        The same ``state`` instance, updated.
    """
    handle = spawn_worker(issue, attempt)
    if handle is None:
        return schedule_retry(
            state,
            issue.id,
            next_attempt(attempt),
            identifier=issue.identifier,
            error="failed to spawn agent",
        )

    run_attempt = RunAttempt(
        issue_id=issue.id,
        issue_identifier=issue.identifier,
        workspace_path=workspace_path_for(issue.identifier, workspace_root),
        started_at=now(),
        attempt=attempt,
        status=RunAttemptPhase.PREPARING_WORKSPACE,
    )
    state.running[issue.id] = RunningEntry(
        run_attempt=run_attempt,
        issue=issue,
        worker_handle=handle,
    )
    state.claimed.add(issue.id)
    state.retry_attempts.pop(issue.id, None)
    return state


# --- Retry & backoff (SPEC §8.4, §16.6) ----------------------------------------

_CONTINUATION_DELAY_MS = 1000
"""Fixed delay for a continuation retry after a clean worker exit (SPEC §8.4)."""

_FAILURE_BASE_DELAY_MS = 10000
"""Base delay for failure-driven exponential backoff (SPEC §8.4)."""

_MAX_BACKOFF_EXPONENT = 30
"""Cap on the backoff power so the raw delay never overflows into huge ints; any
configured ``max_retry_backoff_ms`` is reached far below this (SPEC §8.4)."""


class RetryDelay(Enum):
    """How a retry's delay is computed (SPEC §8.4).

    ``CONTINUATION`` is the short fixed re-check after a clean worker exit;
    ``FAILURE`` is the exponential backoff used for every error-driven retry.
    """

    CONTINUATION = "continuation"
    FAILURE = "failure"


# (issue_id, delay_ms) -> opaque timer handle that fires the retry when due.
SetRetryTimer = Callable[[str, int], object]
# Cancels a previously scheduled retry timer handle.
CancelRetryTimer = Callable[[object], None]
# Fetches the active dispatch candidates (``tracker.fetch_candidate_issues``).
CandidateFetcher = Callable[[], Sequence[Issue]]
# Dispatches an issue at a given attempt (``dispatch_issue`` with its seams bound).
DispatchFn = Callable[[Issue, OrchestratorState, int | None], OrchestratorState]


def _monotonic_ms() -> int:
    """Monotonic clock in milliseconds for retry ``due_at_ms`` (SPEC §4.1.7)."""
    return time.monotonic_ns() // 1_000_000


def compute_backoff_ms(
    attempt: int, *, delay_type: RetryDelay, max_retry_backoff_ms: int
) -> int:
    """Delay before a retry fires (SPEC §8.4).

    Continuation retries use a fixed ``1000`` ms. Failure retries use
    ``min(10000 * 2 ** (attempt - 1), max_retry_backoff_ms)``, so the very first
    failure waits ``10000`` ms and each subsequent attempt doubles up to the cap.
    """
    if delay_type is RetryDelay.CONTINUATION:
        return _CONTINUATION_DELAY_MS
    exponent = min(max(attempt - 1, 0), _MAX_BACKOFF_EXPONENT)
    # ``1 << exponent`` == ``2 ** exponent`` for exponent >= 0, and stays typed int.
    raw_delay = _FAILURE_BASE_DELAY_MS * (1 << exponent)
    return min(raw_delay, max_retry_backoff_ms)


@dataclass(frozen=True, slots=True)
class RetryScheduler:
    """Creates and tracks retry timers, satisfying the ``ScheduleRetry`` seam.

    Implements retry-entry creation (SPEC §8.4): it cancels any existing timer for
    the issue, computes the backoff, arms a new timer through the injected runtime
    seams, and records the :class:`~symphony.models.RetryEntry`. The issue is kept
    claimed so a ``RetryQueued`` issue is never re-dispatched by a concurrent poll
    tick (SPEC §7.1, §7.4) — this also pins down the terse spawn-failure path in
    SPEC §16.4, which routes here without having claimed yet.

    Attributes:
        set_timer: Arms a timer for ``(issue_id, delay_ms)`` and returns a handle.
        cancel_timer: Cancels a previously armed timer handle.
        max_retry_backoff_ms: Cap for failure backoff (``agent.max_retry_backoff_ms``).
        now_ms: Monotonic clock for ``due_at_ms`` (injectable for tests).
    """

    set_timer: SetRetryTimer
    cancel_timer: CancelRetryTimer
    max_retry_backoff_ms: int
    now_ms: Callable[[], int] = _monotonic_ms

    def __call__(
        self,
        state: OrchestratorState,
        issue_id: str,
        attempt: int,
        *,
        identifier: str,
        error: str | None = None,
        delay_type: RetryDelay = RetryDelay.FAILURE,
    ) -> OrchestratorState:
        """Schedule (or reschedule) the retry for ``issue_id`` (SPEC §8.4)."""
        existing = state.retry_attempts.get(issue_id)
        if existing is not None and existing.timer_handle is not None:
            self.cancel_timer(existing.timer_handle)

        delay_ms = compute_backoff_ms(
            attempt,
            delay_type=delay_type,
            max_retry_backoff_ms=self.max_retry_backoff_ms,
        )
        handle = self.set_timer(issue_id, delay_ms)
        state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=self.now_ms() + delay_ms,
            timer_handle=handle,
            error=error,
        )
        state.claimed.add(issue_id)
        return state


def on_retry_timer(
    issue_id: str,
    state: OrchestratorState,
    *,
    fetch_active_issue_candidates: CandidateFetcher,
    dispatch: DispatchFn,
    schedule_retry: ScheduleRetry,
) -> OrchestratorState:
    """Handle a fired retry timer for ``issue_id`` (SPEC §8.4, §16.6).

    Pops the retry entry, then re-checks the tracker's active candidates: a failed
    poll requeues; an absent issue (terminal or no longer active — candidates are
    active-only) releases the claim; otherwise the issue is dispatched if a global
    slot is free, or requeued with ``no available orchestrator slots``.

    Args:
        issue_id: The issue whose retry timer fired.
        state: The authoritative orchestrator state (mutated in place).
        fetch_active_issue_candidates: Active-candidate fetch seam (tracker).
        dispatch: Bound dispatch seam (``dispatch_issue`` with its seams applied).
        schedule_retry: Retry-scheduling seam for the requeue paths.
    """
    retry_entry = state.retry_attempts.pop(issue_id, None)
    if retry_entry is None:
        return state

    try:
        active_issues = fetch_active_issue_candidates()
    except TrackerError:
        return schedule_retry(
            state,
            issue_id,
            retry_entry.attempt + 1,
            identifier=retry_entry.identifier,
            error="retry poll failed",
        )

    issue = next((c for c in active_issues if c.id == issue_id), None)
    if issue is None:
        state.claimed.discard(issue_id)
        return state

    if available_slots(state) <= 0:
        return schedule_retry(
            state,
            issue_id,
            retry_entry.attempt + 1,
            identifier=issue.identifier,
            error="no available orchestrator slots",
        )

    return dispatch(issue, state, retry_entry.attempt)
