"""Tests for orchestrator state primitives and dispatch (SPEC §7, §16.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from symphony.models import (
    Issue,
    OrchestratorState,
    RetryEntry,
    RunAttempt,
    RunAttemptPhase,
    RunningEntry,
)
from symphony.orchestrator import (
    claim,
    dispatch_issue,
    is_claimed,
    is_running,
    mark_completed,
    next_attempt,
    release,
)
from symphony.workspace import workspace_path_for

_FIXED_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _issue(issue_id: str = "i-1", identifier: str = "ABC-1") -> Issue:
    return Issue(id=issue_id, identifier=identifier, title="Fix bug", state="Todo")


def _clock() -> datetime:
    return _FIXED_NOW


class _SpyRetry:
    """Records the spawn-failure retry call and echoes the state back."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        state: OrchestratorState,
        issue_id: str,
        attempt: int,
        *,
        identifier: str,
        error: str | None,
    ) -> OrchestratorState:
        self.calls.append(
            {
                "issue_id": issue_id,
                "attempt": attempt,
                "identifier": identifier,
                "error": error,
            }
        )
        return state


# --- next_attempt --------------------------------------------------------------


def test_next_attempt_escalates_from_none_to_one() -> None:
    assert next_attempt(None) == 1


def test_next_attempt_increments_existing() -> None:
    assert next_attempt(1) == 2
    assert next_attempt(4) == 5


# --- claim / running bookkeeping ----------------------------------------------


def test_claim_and_release_are_idempotent() -> None:
    state = OrchestratorState()
    claim(state, "i-1")
    claim(state, "i-1")
    assert is_claimed(state, "i-1")
    assert state.claimed == {"i-1"}

    release(state, "i-1")
    release(state, "i-1")  # releasing an unclaimed issue is a no-op
    assert not is_claimed(state, "i-1")


def test_is_running_reflects_running_map() -> None:
    state = OrchestratorState()
    assert not is_running(state, "i-1")
    state.running["i-1"] = RunningEntry(
        run_attempt=RunAttempt(
            issue_id="i-1",
            issue_identifier="ABC-1",
            workspace_path=Path("/ws/ABC-1"),
            started_at=_FIXED_NOW,
        )
    )
    assert is_running(state, "i-1")


def test_mark_completed_is_bookkeeping_only() -> None:
    state = OrchestratorState()
    mark_completed(state, "i-1")
    assert "i-1" in state.completed
    # completed does not claim or run the issue (SPEC §7.1)
    assert not is_claimed(state, "i-1")
    assert not is_running(state, "i-1")


# --- dispatch_issue: success path ---------------------------------------------


def test_dispatch_records_running_claim_and_handle(tmp_path: Path) -> None:
    state = OrchestratorState()
    issue = _issue()
    handle = object()
    retry = _SpyRetry()

    result = dispatch_issue(
        issue,
        state,
        attempt=None,
        workspace_root=tmp_path,
        spawn_worker=lambda _issue, _attempt: handle,
        schedule_retry=retry,
        now=_clock,
    )

    assert result is state
    assert retry.calls == []
    assert is_claimed(state, "i-1")
    assert is_running(state, "i-1")

    entry = state.running["i-1"]
    assert entry.worker_handle is handle
    assert entry.monitor_handle is None
    assert entry.session is None

    attempt = entry.run_attempt
    assert attempt.issue_id == "i-1"
    assert attempt.issue_identifier == "ABC-1"
    assert attempt.attempt is None
    assert attempt.status is RunAttemptPhase.PREPARING_WORKSPACE
    assert attempt.started_at == _FIXED_NOW
    assert attempt.workspace_path == workspace_path_for("ABC-1", tmp_path)


def test_dispatch_uses_utc_clock_by_default(tmp_path: Path) -> None:
    state = OrchestratorState()

    dispatch_issue(
        _issue(),
        state,
        attempt=None,
        workspace_root=tmp_path,
        spawn_worker=lambda _i, _a: object(),
        schedule_retry=_SpyRetry(),
    )

    started_at = state.running["i-1"].run_attempt.started_at
    assert started_at.tzinfo is UTC


def test_dispatch_passes_issue_and_attempt_to_spawner(tmp_path: Path) -> None:
    state = OrchestratorState()
    issue = _issue()
    seen: list[tuple[Issue, int | None]] = []

    def spawn(issue: Issue, attempt: int | None) -> object:
        seen.append((issue, attempt))
        return object()

    dispatch_issue(
        issue,
        state,
        attempt=2,
        workspace_root=tmp_path,
        spawn_worker=spawn,
        schedule_retry=_SpyRetry(),
        now=_clock,
    )

    assert seen == [(issue, 2)]
    assert state.running["i-1"].run_attempt.attempt == 2


def test_dispatch_clears_pending_retry_for_issue(tmp_path: Path) -> None:
    state = OrchestratorState()
    state.retry_attempts["i-1"] = RetryEntry(
        issue_id="i-1", identifier="ABC-1", attempt=1, due_at_ms=0
    )

    dispatch_issue(
        _issue(),
        state,
        attempt=1,
        workspace_root=tmp_path,
        spawn_worker=lambda _i, _a: object(),
        schedule_retry=_SpyRetry(),
        now=_clock,
    )

    assert "i-1" not in state.retry_attempts
    assert is_running(state, "i-1")


# --- dispatch_issue: spawn-failure path ---------------------------------------


def test_dispatch_spawn_failure_routes_to_retry_without_running(
    tmp_path: Path,
) -> None:
    state = OrchestratorState()
    retry = _SpyRetry()

    result = dispatch_issue(
        _issue(),
        state,
        attempt=None,
        workspace_root=tmp_path,
        spawn_worker=lambda _i, _a: None,
        schedule_retry=retry,
        now=_clock,
    )

    assert result is state
    assert not is_running(state, "i-1")
    assert not is_claimed(state, "i-1")
    assert retry.calls == [
        {
            "issue_id": "i-1",
            "attempt": 1,
            "identifier": "ABC-1",
            "error": "failed to spawn agent",
        }
    ]


def test_dispatch_spawn_failure_escalates_existing_attempt(tmp_path: Path) -> None:
    state = OrchestratorState()
    retry = _SpyRetry()

    dispatch_issue(
        _issue(),
        state,
        attempt=3,
        workspace_root=tmp_path,
        spawn_worker=lambda _i, _a: None,
        schedule_retry=retry,
        now=_clock,
    )

    assert retry.calls[0]["attempt"] == 4
