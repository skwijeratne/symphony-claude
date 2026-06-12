"""Tests for orchestrator state primitives and dispatch (SPEC §7, §16.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from symphony.config import AgentConfig, ServiceConfig, TrackerConfig
from symphony.models import (
    BlockerRef,
    Issue,
    OrchestratorState,
    RetryEntry,
    RunAttempt,
    RunAttemptPhase,
    RunningEntry,
)
from symphony.orchestrator import (
    available_slots,
    claim,
    dispatch_issue,
    is_claimed,
    is_running,
    mark_completed,
    next_attempt,
    per_state_available_slots,
    release,
    running_count_for_state,
    should_dispatch,
    sort_for_dispatch,
)
from symphony.workspace import workspace_path_for

_FIXED_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _issue(
    issue_id: str = "i-1",
    identifier: str = "ABC-1",
    *,
    state: str = "Todo",
    title: str = "Fix bug",
    labels: list[str] | None = None,
    priority: int | None = None,
    created_at: datetime | None = None,
    blocked_by: list[BlockerRef] | None = None,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=title,
        state=state,
        labels=labels if labels is not None else [],
        priority=priority,
        created_at=created_at,
        blocked_by=blocked_by if blocked_by is not None else [],
    )


def _config(
    *,
    required_labels: list[str] | None = None,
    active_states: list[str] | None = None,
    terminal_states: list[str] | None = None,
    by_state: dict[str, int] | None = None,
) -> ServiceConfig:
    return ServiceConfig(
        tracker=TrackerConfig(
            required_labels=required_labels if required_labels is not None else [],
            active_states=(
                active_states if active_states is not None else ["Todo", "In Progress"]
            ),
            terminal_states=(
                terminal_states if terminal_states is not None else ["Done", "Canceled"]
            ),
        ),
        agent=AgentConfig(
            max_concurrent_agents_by_state=by_state if by_state is not None else {},
        ),
    )


def _running(state: OrchestratorState, issue: Issue) -> None:
    """Register ``issue`` as running for concurrency-count tests."""
    state.running[issue.id] = RunningEntry(
        run_attempt=RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            workspace_path=Path("/ws") / issue.identifier,
            started_at=_FIXED_NOW,
        ),
        issue=issue,
    )


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


# --- concurrency: global + per-state slots (SPEC §8.3) -------------------------


def test_available_slots_floors_at_zero() -> None:
    state = OrchestratorState(max_concurrent_agents=2)
    assert available_slots(state) == 2
    _running(state, _issue("i-1", "ABC-1", state="In Progress"))
    assert available_slots(state) == 1
    _running(state, _issue("i-2", "ABC-2", state="In Progress"))
    _running(state, _issue("i-3", "ABC-3", state="In Progress"))
    assert available_slots(state) == 0  # never negative


def test_running_count_for_state_counts_by_tracked_state() -> None:
    state = OrchestratorState()
    _running(state, _issue("i-1", "ABC-1", state="Todo"))
    _running(state, _issue("i-2", "ABC-2", state="In Progress"))
    _running(state, _issue("i-3", "ABC-3", state="in progress"))  # case-insensitive
    assert running_count_for_state(state, "In Progress") == 2
    assert running_count_for_state(state, "Todo") == 1
    assert running_count_for_state(state, "Done") == 0


def test_per_state_slots_use_override_then_fall_back_to_global() -> None:
    state = OrchestratorState(max_concurrent_agents=5)
    config = _config(by_state={"in progress": 1})
    # Override present for In Progress.
    assert per_state_available_slots(state, config, "In Progress") == 1
    _running(state, _issue("i-1", "ABC-1", state="In Progress"))
    assert per_state_available_slots(state, config, "In Progress") == 0
    # No override for Todo -> falls back to the global limit (minus Todo runners).
    assert per_state_available_slots(state, config, "Todo") == 5


# --- should_dispatch: eligibility predicates (SPEC §8.2) -----------------------


def test_should_dispatch_accepts_eligible_issue() -> None:
    state = OrchestratorState(max_concurrent_agents=2)
    config = _config(required_labels=["bug"])
    issue = _issue(state="In Progress", labels=["bug", "backend"])
    assert should_dispatch(issue, state, config)


def test_should_dispatch_rejects_missing_core_fields() -> None:
    state = OrchestratorState()
    config = _config()
    assert not should_dispatch(_issue(title=""), state, config)
    assert not should_dispatch(_issue(issue_id=""), state, config)


def test_should_dispatch_rejects_inactive_or_terminal_state() -> None:
    state = OrchestratorState()
    config = _config(active_states=["Todo"], terminal_states=["Done"])
    assert not should_dispatch(_issue(state="In Review"), state, config)
    assert not should_dispatch(_issue(state="Done"), state, config)


def test_should_dispatch_requires_all_required_labels() -> None:
    state = OrchestratorState()
    config = _config(required_labels=["bug", "ready"])
    assert not should_dispatch(_issue(labels=["bug"]), state, config)
    assert should_dispatch(_issue(labels=["Bug", "Ready"]), state, config) is False
    # Labels on the issue arrive normalized (lowercased) from the tracker layer.
    assert should_dispatch(_issue(labels=["bug", "ready"]), state, config)


def test_should_dispatch_rejects_running_or_claimed() -> None:
    config = _config()
    running_state = OrchestratorState()
    _running(running_state, _issue())
    assert not should_dispatch(_issue(), running_state, config)

    claimed_state = OrchestratorState()
    claim(claimed_state, "i-1")
    assert not should_dispatch(_issue(), claimed_state, config)


def test_should_dispatch_rejects_when_no_global_slots() -> None:
    state = OrchestratorState(max_concurrent_agents=1)
    config = _config()
    _running(state, _issue("i-9", "ABC-9", state="In Progress"))
    assert not should_dispatch(_issue(), state, config)


def test_should_dispatch_rejects_when_per_state_slots_exhausted() -> None:
    state = OrchestratorState(max_concurrent_agents=5)
    config = _config(by_state={"todo": 1})
    _running(state, _issue("i-9", "ABC-9", state="Todo"))
    assert not should_dispatch(_issue(state="Todo"), state, config)


def test_should_dispatch_todo_blocked_by_nonterminal_blocker() -> None:
    state = OrchestratorState()
    config = _config(terminal_states=["Done"])
    blocked = _issue(blocked_by=[BlockerRef(id="b1", state="In Progress")])
    assert not should_dispatch(blocked, state, config)


def test_should_dispatch_todo_unknown_blocker_state_blocks() -> None:
    state = OrchestratorState()
    config = _config(terminal_states=["Done"])
    blocked = _issue(blocked_by=[BlockerRef(id="b1", state=None)])
    assert not should_dispatch(blocked, state, config)


def test_should_dispatch_todo_allowed_when_blockers_terminal() -> None:
    state = OrchestratorState()
    config = _config(terminal_states=["Done"])
    cleared = _issue(blocked_by=[BlockerRef(id="b1", state="Done")])
    assert should_dispatch(cleared, state, config)


def test_should_dispatch_blocker_rule_only_applies_to_todo() -> None:
    state = OrchestratorState()
    config = _config(active_states=["Todo", "In Progress"], terminal_states=["Done"])
    # Same non-terminal blocker, but the issue is In Progress -> rule does not apply.
    in_progress = _issue(state="In Progress", blocked_by=[BlockerRef(state="Open")])
    assert should_dispatch(in_progress, state, config)


# --- sort_for_dispatch: dispatch order (SPEC §8.2) -----------------------------


def _dt(day: int) -> datetime:
    return datetime(2026, 6, day, tzinfo=UTC)


def test_sort_orders_by_priority_then_created_then_identifier() -> None:
    issues = [
        _issue("i-a", "ABC-3", priority=2, created_at=_dt(2)),
        _issue("i-b", "ABC-1", priority=1, created_at=_dt(5)),
        _issue("i-c", "ABC-2", priority=2, created_at=_dt(1)),
        _issue("i-d", "ABC-4", priority=None, created_at=_dt(1)),
    ]
    ordered = [i.identifier for i in sort_for_dispatch(issues)]
    # priority 1 first; then priority 2 oldest-first (ABC-2 created day 1 before
    # ABC-3 day 2); null priority sorts last.
    assert ordered == ["ABC-1", "ABC-2", "ABC-3", "ABC-4"]


def test_sort_uses_identifier_tiebreaker_and_is_stable() -> None:
    issues = [
        _issue("i-a", "ABC-2", priority=1, created_at=_dt(1)),
        _issue("i-b", "ABC-1", priority=1, created_at=_dt(1)),
    ]
    ordered = [i.identifier for i in sort_for_dispatch(issues)]
    assert ordered == ["ABC-1", "ABC-2"]


def test_sort_places_missing_created_at_last_without_comparing_to_aware() -> None:
    issues = [
        _issue("i-a", "ABC-1", priority=1, created_at=None),
        _issue("i-b", "ABC-2", priority=1, created_at=_dt(9)),
    ]
    ordered = [i.identifier for i in sort_for_dispatch(issues)]
    assert ordered == ["ABC-2", "ABC-1"]
