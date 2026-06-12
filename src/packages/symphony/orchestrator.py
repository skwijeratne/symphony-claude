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

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from symphony.models import (
    Issue,
    OrchestratorState,
    RunAttempt,
    RunAttemptPhase,
    RunningEntry,
)
from symphony.workspace import workspace_path_for

__all__ = [
    "WorkerHandle",
    "WorkerSpawner",
    "ScheduleRetry",
    "next_attempt",
    "is_running",
    "is_claimed",
    "claim",
    "release",
    "mark_completed",
    "dispatch_issue",
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
        worker_handle=handle,
    )
    state.claimed.add(issue.id)
    state.retry_attempts.pop(issue.id, None)
    return state
