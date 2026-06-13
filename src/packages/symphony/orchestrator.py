"""Orchestrator core: scheduling, dispatch, retry, reconciliation (SPEC §7, §8, §16).

The orchestrator is the only component that mutates scheduling state; all worker
outcomes are reported back to it and converted into explicit state transitions
(SPEC §7). This module provides, bottom-up:

- claim / running bookkeeping primitives that guard against duplicate dispatch
  (SPEC §7.1, §7.4),
- candidate selection, sorting, and concurrency (SPEC §8.2-8.3),
- ``dispatch_issue`` (SPEC §16.4), retry/backoff (SPEC §8.4, §16.6), and active-run
  reconciliation (SPEC §8.5, §16.3), and
- the poll-and-dispatch tick, worker-exit accounting, agent-update folding, and
  startup cleanup that compose them (SPEC §7.3, §8.1, §8.6, §13.5, §16.2).

Everything the runtime supplies — spawning/stopping workers, arming timers,
tracker calls, observer notifications, the clock — is an injected seam
(``WorkerSpawner``, ``StopWorker``, ``RetryScheduler``, ``IssueStateRefresher``,
…), so the algorithms here are deterministic and unit-tested without a real event
loop. Wiring those seams to the concrete runtime is M7.

State mutations are applied in place on the single authoritative
:class:`~symphony.models.OrchestratorState` and the same instance is returned, so
callers can chain steps in the functional style of the SPEC §16 reference
algorithms.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from symphony.agent_runner import AttemptResult
from symphony.config import ServiceConfig
from symphony.exceptions import InvalidWorkspacePathError, TrackerError
from symphony.hooks import HookKind, run_hook
from symphony.models import (
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunAttempt,
    RunAttemptPhase,
    RunningEntry,
)
from symphony.normalization import normalize_label, normalize_state
from symphony.stream_parser import AgentEvent, AgentEventType
from symphony.structured_logging import log_fields
from symphony.workspace import remove_workspace, workspace_path_for

__all__ = [
    "WorkerHandle",
    "WorkerSpawner",
    "ScheduleRetry",
    "StopWorker",
    "IssueStateRefresher",
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
    "available_worker_slots",
    "running_count_for_state",
    "per_state_available_worker_slots",
    "should_dispatch",
    "sort_for_dispatch",
    "dispatch_issue",
    "compute_backoff_ms",
    "on_retry_timer",
    "terminate_running_issue",
    "reconcile_stalled_runs",
    "reconcile_running_issues",
    "Reconcile",
    "ValidateDispatch",
    "NotifyObservers",
    "run_tick",
    "on_worker_exit",
    "on_agent_update",
    "startup_terminal_workspace_cleanup",
]

logger = logging.getLogger("symphony.orchestrator")

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

    Backoff timing, timer handling and worker-slot-exhaustion requeue are defined
    in SPEC §8.4; callers depend only on this minimal contract.
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


class StopWorker(Protocol):
    """Stops a running worker during reconciliation (SPEC §8.5).

    Given the live :class:`~symphony.models.RunningEntry`, the runtime cancels the
    worker task / kills the agent subprocess using its handles. The concrete
    mechanism is wired with the event loop (M7); reconciliation only needs to
    signal "stop this one".
    """

    def __call__(self, entry: RunningEntry) -> None: ...


class IssueStateRefresher(Protocol):
    """Fetches current tracker states for running issues (SPEC §11.1, §16.3).

    Satisfied by ``LinearClient.fetch_issue_states_by_ids``; mirrors the agent
    runner's same-named seam so reconciliation does not depend on the worker.
    """

    def fetch_issue_states_by_ids(self, issue_ids: Sequence[str]) -> list[Issue]: ...


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


def available_worker_slots(state: OrchestratorState) -> int:
    """Global worker slots still free (SPEC §8.3).

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


def per_state_available_worker_slots(
    state: OrchestratorState, policy: DispatchPolicy, state_name: str
) -> int:
    """Per-state worker slots still free for ``state_name`` (SPEC §8.3).

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
    not-claimed claim guards (SPEC §7.1), global and per-state worker slots,
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

    if available_worker_slots(state) <= 0:
        return False
    if per_state_available_worker_slots(state, policy, issue.state) <= 0:
        return False

    # The blocker rule only applies to Todo issues (SPEC §8.2).
    return issue_state != "todo" or _todo_blockers_clear(issue, policy.terminal_states)


def _dispatch_sort_key(issue: Issue) -> tuple[bool, int, bool, datetime, str]:
    """Sort key implementing the dispatch order (SPEC §8.2).

    ``priority`` ascending (null last), then ``created_at`` oldest first (null
    last), then ``identifier`` lexicographically. The boolean null-flags guard the
    value positions so the ``datetime.min`` placeholder is only ever compared against
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

    Eligibility (running/claimed/worker-slot checks, SPEC §8.2-8.3) is the caller's
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
        logger.warning(
            "dispatch failed, retrying %s",
            log_fields(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                attempt=attempt,
                reason="failed to spawn agent",
            ),
        )
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
    logger.info(
        "dispatch completed %s",
        log_fields(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt,
        ),
    )
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
        logger.info(
            "retry scheduled %s",
            log_fields(
                issue_id=issue_id,
                issue_identifier=identifier,
                attempt=attempt,
                delay_ms=delay_ms,
                reason=error,
            ),
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
    worker slot is free, or requeued with ``no available orchestrator slots``.

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
        logger.info(
            "retry released %s",
            log_fields(
                issue_id=issue_id,
                issue_identifier=retry_entry.identifier,
                reason="issue no longer an active candidate",
            ),
        )
        return state

    if available_worker_slots(state) <= 0:
        return schedule_retry(
            state,
            issue_id,
            retry_entry.attempt + 1,
            identifier=issue.identifier,
            error="no available orchestrator slots",
        )

    return dispatch(issue, state, retry_entry.attempt)


# --- Active-run reconciliation (SPEC §8.5, §16.3) ------------------------------


def _stall_reference(entry: RunningEntry) -> datetime:
    """The instant a running worker's stall clock is measured from (SPEC §8.5).

    The last ``stream-json`` event timestamp if one has been seen, else the
    attempt's ``started_at``.
    """
    session = entry.session
    if session is not None and session.last_event_timestamp is not None:
        return session.last_event_timestamp
    return entry.run_attempt.started_at


def terminate_running_issue(
    state: OrchestratorState,
    issue_id: str,
    *,
    cleanup_workspace: bool,
    config: ServiceConfig,
    stop_worker: StopWorker,
) -> OrchestratorState:
    """Stop a running worker and drop its claim (SPEC §8.5, §16.3).

    Removes the issue from ``running``, stops the worker via the ``stop_worker``
    seam, and releases the claim (the issue is terminal, non-active, or being
    requeued by the caller). When ``cleanup_workspace`` is set, the ``before_remove``
    hook runs (best-effort, §9.4) and the workspace directory is removed (§8.5);
    otherwise the workspace is left in place for a later run.

    A no-op if ``issue_id`` is not currently running.
    """
    entry = state.running.pop(issue_id, None)
    if entry is None:
        return state

    stop_worker(entry)
    logger.info(
        "running issue terminated %s",
        log_fields(
            issue_id=issue_id,
            issue_identifier=entry.run_attempt.issue_identifier,
            session_id=entry.session.session_id if entry.session else None,
            cleanup_workspace=cleanup_workspace,
        ),
    )

    if cleanup_workspace and config.workspace.root is not None:
        workspace_path = entry.run_attempt.workspace_path
        run_hook(HookKind.BEFORE_REMOVE, config.hooks, workspace_path)
        remove_workspace(workspace_path, config.workspace.root)

    state.claimed.discard(issue_id)
    return state


def reconcile_stalled_runs(
    state: OrchestratorState,
    *,
    config: ServiceConfig,
    stop_worker: StopWorker,
    schedule_retry: ScheduleRetry,
    now: Callable[[], datetime] = _utcnow,
) -> OrchestratorState:
    """Stall detection — SPEC §8.5 Part A.

    For each running worker, if the time since its last ``stream-json`` event (or
    its start, if none) exceeds ``claude.stall_timeout_ms``, stop the worker and
    queue a failure retry — the issue is still active, just stuck. Disabled when
    ``stall_timeout_ms <= 0``.
    """
    stall_timeout_ms = config.claude.stall_timeout_ms
    if stall_timeout_ms <= 0:
        return state

    now_dt = now()
    for issue_id, entry in list(state.running.items()):
        elapsed_ms = (now_dt - _stall_reference(entry)).total_seconds() * 1000
        if elapsed_ms <= stall_timeout_ms:
            continue
        identifier = entry.run_attempt.issue_identifier
        attempt = next_attempt(entry.run_attempt.attempt)
        logger.warning(
            "worker stalled, retrying %s",
            log_fields(
                issue_id=issue_id,
                issue_identifier=identifier,
                session_id=entry.session.session_id if entry.session else None,
                elapsed_ms=int(elapsed_ms),
                stall_timeout_ms=stall_timeout_ms,
            ),
        )
        state = terminate_running_issue(
            state,
            issue_id,
            cleanup_workspace=False,
            config=config,
            stop_worker=stop_worker,
        )
        state = schedule_retry(
            state, issue_id, attempt, identifier=identifier, error="worker stalled"
        )
    return state


def reconcile_running_issues(
    state: OrchestratorState,
    *,
    config: ServiceConfig,
    policy: DispatchPolicy,
    tracker: IssueStateRefresher,
    stop_worker: StopWorker,
    schedule_retry: ScheduleRetry,
    now: Callable[[], datetime] = _utcnow,
) -> OrchestratorState:
    """Reconcile active runs against the tracker — SPEC §8.5, §16.3.

    Runs stall detection (Part A), then refreshes the tracker state of every
    running issue (Part B): terminal issues are stopped and their workspace cleaned;
    issues still active have their in-memory snapshot updated; issues that are
    neither active nor terminal are stopped without workspace cleanup. A failed
    state refresh keeps all workers running and is retried next tick.

    ``policy`` supplies the same normalized active/terminal state sets the poll
    tick already built for dispatch (:class:`DispatchPolicy`), so both decisions
    share one definition of "active"/"terminal" rather than re-deriving it here.
    """
    updated_state = reconcile_stalled_runs(
        state,
        config=config,
        stop_worker=stop_worker,
        schedule_retry=schedule_retry,
        now=now,
    )

    running_ids = list(updated_state.running.keys())
    if not running_ids:
        return updated_state

    try:
        refreshed_issues = tracker.fetch_issue_states_by_ids(running_ids)
    except TrackerError:
        return updated_state  # keep workers running; try again next tick

    for issue in refreshed_issues:
        issue_state = issue.normalized_state
        if issue_state in policy.terminal_states:
            updated_state = terminate_running_issue(
                updated_state,
                issue.id,
                cleanup_workspace=True,
                config=config,
                stop_worker=stop_worker,
            )
        elif issue_state in policy.active_states:
            worker_entry = updated_state.running.get(issue.id)
            if worker_entry is not None:
                worker_entry.issue = issue
        else:
            updated_state = terminate_running_issue(
                updated_state,
                issue.id,
                cleanup_workspace=False,
                config=config,
                stop_worker=stop_worker,
            )
    return updated_state


# --- Poll-and-dispatch tick (SPEC §8.1, §16.2) ---------------------------------

# Reconcile active runs and return the updated state (`reconcile_running_issues`
# with its seams bound).
Reconcile = Callable[[OrchestratorState], OrchestratorState]
# Per-tick dispatch preflight (SPEC §6.3); ``True`` when dispatch may proceed.
ValidateDispatch = Callable[[], bool]
# Notifies observability/status consumers of a state change (SPEC §8.1 step 6).
NotifyObservers = Callable[[OrchestratorState], None]


def run_tick(
    state: OrchestratorState,
    *,
    policy: DispatchPolicy,
    reconcile_issues: Reconcile,
    validate_dispatch_config: ValidateDispatch,
    fetch_active_issue_candidates: CandidateFetcher,
    dispatch: DispatchFn,
    notify: NotifyObservers | None = None,
) -> OrchestratorState:
    """Run one poll-and-dispatch tick (SPEC §8.1, §16.2).

    The sequence is fixed: reconcile running issues first, then validate dispatch
    config, fetch candidates, and dispatch the eligible ones in priority order
    until the worker slots run out. Reconciliation happens on every tick; a failed
    validation or candidate fetch skips dispatch for this tick only. Scheduling the
    next tick is the event loop's job (M7).

    Args:
        state: The authoritative orchestrator state (mutated in place).
        policy: Precomputed selection criteria, shared with reconciliation.
        reconcile_issues: Active-run reconciliation seam (SPEC §16.3).
        validate_dispatch_config: Per-tick dispatch preflight (SPEC §6.3);
            ``False`` skips dispatch.
        fetch_active_issue_candidates: Active-candidate fetch seam (tracker).
        dispatch: First-dispatch seam (``dispatch_issue`` with its seams bound).
        notify: Optional observer-notification seam.
    """
    state = reconcile_issues(state)

    if not validate_dispatch_config():
        _notify(notify, state)
        return state

    try:
        candidates = fetch_active_issue_candidates()
    except TrackerError:
        _notify(notify, state)
        return state

    for issue in sort_for_dispatch(candidates):
        if available_worker_slots(state) <= 0:
            break
        if should_dispatch(issue, state, policy):
            state = dispatch(issue, state, None)

    _notify(notify, state)
    return state


def _notify(notify: NotifyObservers | None, state: OrchestratorState) -> None:
    if notify is not None:
        notify(state)


# --- Worker exit + accounting (SPEC §13.5, §16.6) ------------------------------


def on_worker_exit(
    state: OrchestratorState,
    issue_id: str,
    result: AttemptResult,
    *,
    schedule_retry: RetryScheduler,
    now: Callable[[], datetime] = _utcnow,
    notify: NotifyObservers | None = None,
) -> OrchestratorState:
    """Fold a finished worker back into state (SPEC §13.5, §16.6).

    Removes the running entry, accumulates its runtime and token/cost totals into
    ``agent_totals`` (SPEC §13.5), and schedules a follow-up retry: a clean exit
    queues a short *continuation* retry (attempt ``1``) to re-check whether the
    issue still needs work, while a failed attempt queues an exponential-backoff
    retry at the next attempt number (SPEC §7.3, §16.6).

    A no-op if ``issue_id`` is not running (for example it was already terminated
    by reconciliation).
    """
    entry = state.running.pop(issue_id, None)
    if entry is None:
        return state

    _accumulate_totals(state, entry, result, now())
    identifier = entry.run_attempt.issue_identifier
    logger.info(
        "worker exited %s",
        log_fields(
            issue_id=issue_id,
            issue_identifier=identifier,
            session_id=result.session_id,
            outcome="completed" if result.succeeded else "failed",
            turns=result.turns,
            total_tokens=result.total_tokens,
            reason=result.error,
        ),
    )

    if result.succeeded:
        state.completed.add(issue_id)
        schedule_retry(
            state,
            issue_id,
            1,
            identifier=identifier,
            delay_type=RetryDelay.CONTINUATION,
        )
    else:
        schedule_retry(
            state,
            issue_id,
            next_attempt(entry.run_attempt.attempt),
            identifier=identifier,
            error=f"worker exited: {result.error}",
            delay_type=RetryDelay.FAILURE,
        )

    _notify(notify, state)
    return state


def _accumulate_totals(
    state: OrchestratorState,
    entry: RunningEntry,
    result: AttemptResult,
    now_dt: datetime,
) -> None:
    """Add one finished attempt's runtime and tokens to ``agent_totals`` (§13.5)."""
    totals = state.agent_totals
    elapsed_s = (now_dt - entry.run_attempt.started_at).total_seconds()
    totals.runtime_seconds += max(elapsed_s, 0.0)
    totals.input_tokens += result.input_tokens
    totals.output_tokens += result.output_tokens
    totals.total_tokens += result.total_tokens
    if result.cost_usd is not None:
        totals.total_cost_usd += result.cost_usd


# --- Agent updates: live session + rate limits (SPEC §7.3, §13.5) ---------------

_RATE_LIMIT_CATEGORIES = frozenset({"rate_limit", "overloaded"})
"""``api_retry`` error categories that indicate rate limiting (SPEC §13.5)."""

_LAST_MESSAGE_MAX_CHARS = 200
"""Cap on the stored last-event summary, so state never holds large payloads
(SPEC §13.1 discourages keeping raw payloads around)."""


def _as_int(value: object) -> int:
    """Lenient int extraction for usage fields (SPEC §13.5)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _summarize_event(event: AgentEvent) -> str | None:
    """A compact human summary of an event payload, or ``None`` when it has none."""
    payload = event.raw.get("result") or event.raw.get("message")
    if payload is None:
        return None
    return str(payload)[:_LAST_MESSAGE_MAX_CHARS]


def _track_rate_limits(
    state: OrchestratorState, event: AgentEvent, now_dt: datetime
) -> None:
    """Fold one event's rate-limit / API-retry signal into state (SPEC §13.5).

    Tracks the *latest* signal: an ``api_retry`` event updates the snapshot from
    its error category/status, and a terminal ``result`` carrying
    ``api_error_status`` records that status. Other events leave the snapshot
    untouched.
    """
    limits = state.agent_rate_limits
    if event.type is AgentEventType.API_RETRY:
        error = event.raw.get("error")
        error_map = error if isinstance(error, dict) else {}
        limits.is_rate_limited = error_map.get("category") in _RATE_LIMIT_CATEGORIES
        retry_after = _as_int(event.raw.get("retry_after_ms"))
        limits.retry_after_ms = retry_after if retry_after > 0 else None
        status = _as_int(error_map.get("error_status"))
        if status > 0:
            limits.last_api_error_status = status
        limits.updated_at = now_dt
    elif event.is_terminal:
        status = _as_int(event.raw.get("api_error_status"))
        if status > 0:
            limits.last_api_error_status = status
            limits.updated_at = now_dt


def on_agent_update(
    state: OrchestratorState,
    issue_id: str,
    event: AgentEvent,
    *,
    now: Callable[[], datetime] = _utcnow,
) -> OrchestratorState:
    """Fold one agent ``stream-json`` event into state (SPEC §7.3, §13.5).

    Updates the running entry's :class:`~symphony.models.LiveSession` — created
    on the first event that carries a ``session_id`` — with the last-event
    fields, per-run token counters (added once per terminal ``result``, per
    SPEC §13.5), and the turn counter (one per ``session_started``). The
    rate-limit / API-retry snapshot is tracked from any agent update, even one
    whose worker is no longer running.

    Aggregate ``agent_totals`` are intentionally *not* touched here: the
    attempt's totals are folded in once by :func:`on_worker_exit`, so per-event
    accounting feeds only the live session view.
    """
    _track_rate_limits(state, event, now())

    entry = state.running.get(issue_id)
    if entry is None:
        return state

    session = entry.session
    if session is None:
        if event.session_id is None:
            return state  # no session yet and this event cannot start one
        session = LiveSession(session_id=event.session_id)
        entry.session = session
        logger.info(
            "agent session started %s",
            log_fields(
                issue_id=issue_id,
                issue_identifier=entry.run_attempt.issue_identifier,
                session_id=session.session_id,
            ),
        )

    if event.type is AgentEventType.SESSION_STARTED:
        session.turn_count += 1

    session.last_event = (
        f"{event.type.value}/{event.subtype}" if event.subtype else event.type.value
    )
    session.last_event_timestamp = now()
    summary = _summarize_event(event)
    if summary is not None:
        session.last_message = summary

    if event.is_terminal:
        usage = event.raw.get("usage")
        usage_map = usage if isinstance(usage, dict) else {}
        input_tokens = _as_int(usage_map.get("input_tokens"))
        output_tokens = _as_int(usage_map.get("output_tokens"))
        session.input_tokens += input_tokens
        session.output_tokens += output_tokens
        # Derive the total as input + output; the CLI usage map has no total field.
        session.total_tokens += input_tokens + output_tokens
        cost = event.raw.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            session.last_cost_usd = float(cost)

    return state


# --- Startup terminal-workspace cleanup (SPEC §8.6) ----------------------------


def startup_terminal_workspace_cleanup(
    *,
    config: ServiceConfig,
    fetch_terminal_issues: CandidateFetcher,
) -> int:
    """Remove stale workspaces for terminal issues at startup (SPEC §8.6).

    Queries the tracker for issues in terminal states and removes each one's
    workspace directory, so workspaces for already-finished issues do not pile up
    across restarts. Best-effort throughout: a failed fetch logs nothing here and
    simply continues (returns 0), a missing workspace is skipped, and an identifier
    that would escape the root is ignored rather than risking a bad delete.

    Args:
        config: The resolved service configuration (workspace root).
        fetch_terminal_issues: Seam returning the tracker's terminal-state issues
            (``tracker.fetch_issues_by_states(terminal_states)`` bound).

    Returns:
        The number of workspace directories removed.
    """
    root = config.workspace.root
    if root is None:
        return 0

    try:
        issues = fetch_terminal_issues()
    except TrackerError as error:
        # Warn + continue: startup cleanup is best-effort (SPEC §8.6).
        logger.warning(
            "startup workspace cleanup skipped %s",
            log_fields(reason="terminal-issue fetch failed", error=error),
        )
        return 0

    removed = 0
    for issue in issues:
        try:
            path = workspace_path_for(issue.identifier, root)
        except InvalidWorkspacePathError:
            continue
        if remove_workspace(path, root):
            removed += 1
    logger.info("startup workspace cleanup completed %s", log_fields(removed=removed))
    return removed
