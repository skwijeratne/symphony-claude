"""Tests for orchestrator state primitives and dispatch (SPEC §7, §16.4)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    HooksConfig,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from symphony.exceptions import LinearApiRequestError
from symphony.models import (
    BlockerRef,
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunAttempt,
    RunAttemptPhase,
    RunningEntry,
)
from symphony.orchestrator import (
    DispatchFn,
    DispatchPolicy,
    RetryDelay,
    RetryScheduler,
    available_worker_slots,
    claim,
    compute_backoff_ms,
    dispatch_issue,
    is_claimed,
    is_running,
    mark_completed,
    next_attempt,
    on_retry_timer,
    per_state_available_worker_slots,
    reconcile_running_issues,
    reconcile_stalled_runs,
    release,
    running_count_for_state,
    should_dispatch,
    sort_for_dispatch,
    terminate_running_issue,
)
from symphony.workspace import ensure_workspace, workspace_path_for

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


def _policy(
    *,
    required_labels: list[str] | None = None,
    active_states: list[str] | None = None,
    terminal_states: list[str] | None = None,
    by_state: dict[str, int] | None = None,
) -> DispatchPolicy:
    config = ServiceConfig(
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
    return DispatchPolicy.from_config(config)


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


# --- concurrency: global + per-state worker slots (SPEC §8.3) -------------------------


def test_available_worker_slots_floors_at_zero() -> None:
    state = OrchestratorState(max_concurrent_agents=2)
    assert available_worker_slots(state) == 2
    _running(state, _issue("i-1", "ABC-1", state="In Progress"))
    assert available_worker_slots(state) == 1
    _running(state, _issue("i-2", "ABC-2", state="In Progress"))
    _running(state, _issue("i-3", "ABC-3", state="In Progress"))
    assert available_worker_slots(state) == 0  # never negative


def test_running_count_for_state_counts_by_tracked_state() -> None:
    state = OrchestratorState()
    _running(state, _issue("i-1", "ABC-1", state="Todo"))
    _running(state, _issue("i-2", "ABC-2", state="In Progress"))
    _running(state, _issue("i-3", "ABC-3", state="in progress"))  # case-insensitive
    assert running_count_for_state(state, "In Progress") == 2
    assert running_count_for_state(state, "Todo") == 1
    assert running_count_for_state(state, "Done") == 0


def test_per_state_worker_slots_use_override_then_fall_back_to_global() -> None:
    state = OrchestratorState(max_concurrent_agents=5)
    policy = _policy(by_state={"in progress": 1})
    # Override present for In Progress.
    assert per_state_available_worker_slots(state, policy, "In Progress") == 1
    _running(state, _issue("i-1", "ABC-1", state="In Progress"))
    assert per_state_available_worker_slots(state, policy, "In Progress") == 0
    # No override for Todo -> falls back to the global limit (minus Todo runners).
    assert per_state_available_worker_slots(state, policy, "Todo") == 5


# --- DispatchPolicy: precomputed, normalized criteria (SPEC §8.2-8.3) ----------


def test_dispatch_policy_from_config_normalizes_once() -> None:
    config = ServiceConfig(
        tracker=TrackerConfig(
            required_labels=["  Bug  ", "READY"],
            active_states=["Todo", "In Progress"],
            terminal_states=["Done", "Canceled"],
        ),
        agent=AgentConfig(max_concurrent_agents_by_state={"todo": 2}),
    )
    policy = DispatchPolicy.from_config(config)

    assert policy.active_states == frozenset({"todo", "in progress"})
    assert policy.terminal_states == frozenset({"done", "canceled"})
    assert policy.required_labels == frozenset({"bug", "ready"})
    assert policy.max_concurrent_by_state == {"todo": 2}


# --- should_dispatch: eligibility predicates (SPEC §8.2) -----------------------


def test_should_dispatch_accepts_eligible_issue() -> None:
    state = OrchestratorState(max_concurrent_agents=2)
    policy = _policy(required_labels=["bug"])
    issue = _issue(state="In Progress", labels=["bug", "backend"])
    assert should_dispatch(issue, state, policy)


def test_should_dispatch_rejects_missing_core_fields() -> None:
    state = OrchestratorState()
    policy = _policy()
    assert not should_dispatch(_issue(title=""), state, policy)
    assert not should_dispatch(_issue(issue_id=""), state, policy)


def test_should_dispatch_rejects_inactive_or_terminal_state() -> None:
    state = OrchestratorState()
    policy = _policy(active_states=["Todo"], terminal_states=["Done"])
    assert not should_dispatch(_issue(state="In Review"), state, policy)
    assert not should_dispatch(_issue(state="Done"), state, policy)


def test_should_dispatch_requires_all_required_labels() -> None:
    state = OrchestratorState()
    policy = _policy(required_labels=["bug", "ready"])
    assert not should_dispatch(_issue(labels=["bug"]), state, policy)
    assert should_dispatch(_issue(labels=["Bug", "Ready"]), state, policy) is False
    # Labels on the issue arrive normalized (lowercased) from the tracker layer.
    assert should_dispatch(_issue(labels=["bug", "ready"]), state, policy)


def test_should_dispatch_rejects_running_or_claimed() -> None:
    policy = _policy()
    running_state = OrchestratorState()
    _running(running_state, _issue())
    assert not should_dispatch(_issue(), running_state, policy)

    claimed_state = OrchestratorState()
    claim(claimed_state, "i-1")
    assert not should_dispatch(_issue(), claimed_state, policy)


def test_should_dispatch_rejects_when_no_global_worker_slots() -> None:
    state = OrchestratorState(max_concurrent_agents=1)
    policy = _policy()
    _running(state, _issue("i-9", "ABC-9", state="In Progress"))
    assert not should_dispatch(_issue(), state, policy)


def test_should_dispatch_rejects_when_per_state_worker_slots_exhausted() -> None:
    state = OrchestratorState(max_concurrent_agents=5)
    policy = _policy(by_state={"todo": 1})
    _running(state, _issue("i-9", "ABC-9", state="Todo"))
    assert not should_dispatch(_issue(state="Todo"), state, policy)


def test_should_dispatch_todo_blocked_by_nonterminal_blocker() -> None:
    state = OrchestratorState()
    policy = _policy(terminal_states=["Done"])
    blocked = _issue(blocked_by=[BlockerRef(id="b1", state="In Progress")])
    assert not should_dispatch(blocked, state, policy)


def test_should_dispatch_todo_unknown_blocker_state_blocks() -> None:
    state = OrchestratorState()
    policy = _policy(terminal_states=["Done"])
    blocked = _issue(blocked_by=[BlockerRef(id="b1", state=None)])
    assert not should_dispatch(blocked, state, policy)


def test_should_dispatch_todo_allowed_when_blockers_terminal() -> None:
    state = OrchestratorState()
    policy = _policy(terminal_states=["Done"])
    cleared = _issue(blocked_by=[BlockerRef(id="b1", state="Done")])
    assert should_dispatch(cleared, state, policy)


def test_should_dispatch_blocker_rule_only_applies_to_todo() -> None:
    state = OrchestratorState()
    policy = _policy(active_states=["Todo", "In Progress"], terminal_states=["Done"])
    # Same non-terminal blocker, but the issue is In Progress -> rule does not apply.
    in_progress = _issue(state="In Progress", blocked_by=[BlockerRef(state="Open")])
    assert should_dispatch(in_progress, state, policy)


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


# --- compute_backoff_ms (SPEC §8.4) --------------------------------------------


def test_backoff_continuation_is_fixed_one_second() -> None:
    # Attempt number is irrelevant for continuation retries.
    for attempt in (1, 2, 9):
        assert (
            compute_backoff_ms(
                attempt,
                delay_type=RetryDelay.CONTINUATION,
                max_retry_backoff_ms=300000,
            )
            == 1000
        )


def test_backoff_failure_doubles_until_capped() -> None:
    cap = 300000

    def failure(attempt: int) -> int:
        return compute_backoff_ms(
            attempt, delay_type=RetryDelay.FAILURE, max_retry_backoff_ms=cap
        )

    assert failure(1) == 10000  # 10000 * 2^0
    assert failure(2) == 20000  # 10000 * 2^1
    assert failure(3) == 40000  # 10000 * 2^2
    assert failure(5) == 160000  # 10000 * 2^4
    assert failure(6) == cap  # 320000 -> capped at 300000
    assert failure(100) == cap  # large attempt stays capped, no overflow


# --- RetryScheduler (SPEC §8.4) ------------------------------------------------


class _FakeTimers:
    """Records armed/cancelled retry timers for assertions."""

    def __init__(self) -> None:
        self.armed: list[tuple[str, int]] = []
        self.cancelled: list[object] = []
        self._next = 0

    def set(self, issue_id: str, delay_ms: int) -> object:
        self.armed.append((issue_id, delay_ms))
        handle = f"timer-{self._next}"
        self._next += 1
        return handle

    def cancel(self, handle: object) -> None:
        self.cancelled.append(handle)


def _scheduler(timers: _FakeTimers, *, cap: int = 300000) -> RetryScheduler:
    return RetryScheduler(
        set_timer=timers.set,
        cancel_timer=timers.cancel,
        max_retry_backoff_ms=cap,
        now_ms=lambda: 1_000,
    )


def test_retry_scheduler_records_entry_arms_timer_and_claims() -> None:
    timers = _FakeTimers()
    schedule = _scheduler(timers)
    state = OrchestratorState()

    schedule(state, "i-1", 2, identifier="ABC-1", error="boom")

    assert timers.armed == [("i-1", 20000)]
    entry = state.retry_attempts["i-1"]
    assert entry.attempt == 2
    assert entry.identifier == "ABC-1"
    assert entry.error == "boom"
    assert entry.due_at_ms == 1_000 + 20000
    assert entry.timer_handle == "timer-0"
    # A RetryQueued issue stays claimed so a concurrent tick can't re-dispatch it.
    assert is_claimed(state, "i-1")


def test_retry_scheduler_uses_monotonic_clock_by_default() -> None:
    timers = _FakeTimers()
    schedule = RetryScheduler(
        set_timer=timers.set,
        cancel_timer=timers.cancel,
        max_retry_backoff_ms=300000,
    )
    state = OrchestratorState()

    schedule(state, "i-1", 1, identifier="ABC-1", error="boom")

    # due_at = monotonic_now (>= 0) + the 10s backoff, so it is at least 10000.
    assert state.retry_attempts["i-1"].due_at_ms >= 10000


def test_retry_scheduler_continuation_uses_one_second() -> None:
    timers = _FakeTimers()
    schedule = _scheduler(timers)
    state = OrchestratorState()

    schedule(state, "i-1", 1, identifier="ABC-1", delay_type=RetryDelay.CONTINUATION)

    assert timers.armed == [("i-1", 1000)]
    assert state.retry_attempts["i-1"].error is None


def test_retry_scheduler_cancels_existing_timer_on_reschedule() -> None:
    timers = _FakeTimers()
    schedule = _scheduler(timers)
    state = OrchestratorState()

    schedule(state, "i-1", 1, identifier="ABC-1", error="first")
    schedule(state, "i-1", 2, identifier="ABC-1", error="second")

    assert timers.cancelled == ["timer-0"]  # the first timer was cancelled
    assert state.retry_attempts["i-1"].timer_handle == "timer-1"
    assert state.retry_attempts["i-1"].attempt == 2


# --- on_retry_timer (SPEC §8.4, §16.6) -----------------------------------------


def _dispatch_spy() -> tuple[list[tuple[str, int | None]], DispatchFn]:
    calls: list[tuple[str, int | None]] = []

    def dispatch(
        issue: Issue, state: OrchestratorState, attempt: int | None
    ) -> OrchestratorState:
        calls.append((issue.id, attempt))
        state.running[issue.id] = RunningEntry(
            run_attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                workspace_path=Path("/ws") / issue.identifier,
                started_at=_FIXED_NOW,
            ),
            issue=issue,
        )
        return state

    return calls, dispatch


def _queue_retry(state: OrchestratorState, issue_id: str, attempt: int) -> None:
    state.retry_attempts[issue_id] = RetryEntry(
        issue_id=issue_id, identifier="ABC-1", attempt=attempt, due_at_ms=0
    )
    state.claimed.add(issue_id)


def test_on_retry_timer_missing_entry_is_noop() -> None:
    state = OrchestratorState()
    calls, dispatch = _dispatch_spy()

    on_retry_timer(
        "i-1",
        state,
        fetch_active_issue_candidates=lambda: [],
        dispatch=dispatch,
        schedule_retry=_scheduler(_FakeTimers()),
    )

    assert calls == []
    assert state.retry_attempts == {}


def test_on_retry_timer_dispatches_eligible_issue_at_stored_attempt() -> None:
    state = OrchestratorState(max_concurrent_agents=1)
    _queue_retry(state, "i-1", attempt=2)
    calls, dispatch = _dispatch_spy()
    issue = _issue(state="In Progress")

    on_retry_timer(
        "i-1",
        state,
        fetch_active_issue_candidates=lambda: [issue],
        dispatch=dispatch,
        schedule_retry=_scheduler(_FakeTimers()),
    )

    assert calls == [("i-1", 2)]  # re-dispatched at the retry's attempt
    assert "i-1" not in state.retry_attempts


def test_on_retry_timer_releases_claim_when_issue_absent() -> None:
    state = OrchestratorState()
    _queue_retry(state, "i-1", attempt=1)
    calls, dispatch = _dispatch_spy()

    # i-1 is not among the returned candidates.
    on_retry_timer(
        "i-1",
        state,
        fetch_active_issue_candidates=lambda: [_issue("i-2", "ABC-2")],
        dispatch=dispatch,
        schedule_retry=_scheduler(_FakeTimers()),
    )

    assert calls == []
    assert not is_claimed(state, "i-1")  # claim released
    assert "i-1" not in state.retry_attempts


def test_on_retry_timer_requeues_when_no_worker_slots() -> None:
    state = OrchestratorState(max_concurrent_agents=1)
    # The only worker slot is already filled.
    _running(state, _issue("i-busy", "ABC-9", state="In Progress"))
    _queue_retry(state, "i-1", attempt=2)
    calls, dispatch = _dispatch_spy()
    timers = _FakeTimers()

    on_retry_timer(
        "i-1",
        state,
        fetch_active_issue_candidates=lambda: [_issue(state="In Progress")],
        dispatch=dispatch,
        schedule_retry=_scheduler(timers),
    )

    assert calls == []
    requeued = state.retry_attempts["i-1"]
    assert requeued.attempt == 3  # attempt + 1
    assert requeued.error == "no available orchestrator slots"
    assert timers.armed == [("i-1", 40000)]  # backoff for attempt 3


def test_on_retry_timer_requeues_when_candidate_fetch_fails() -> None:
    state = OrchestratorState(max_concurrent_agents=2)
    _queue_retry(state, "i-1", attempt=1)
    calls, dispatch = _dispatch_spy()
    timers = _FakeTimers()

    def failing_fetch() -> list[Issue]:
        raise LinearApiRequestError("network down")

    on_retry_timer(
        "i-1",
        state,
        fetch_active_issue_candidates=failing_fetch,
        dispatch=dispatch,
        schedule_retry=_scheduler(timers),
    )

    assert calls == []
    requeued = state.retry_attempts["i-1"]
    assert requeued.attempt == 2
    assert requeued.error == "retry poll failed"


# --- reconciliation helpers (SPEC §8.5, §16.3) ---------------------------------


def _service_config(
    tmp_path: Path,
    *,
    active: list[str] | None = None,
    terminal: list[str] | None = None,
    stall_timeout_ms: int = 300000,
    before_remove: str | None = None,
    no_root: bool = False,
) -> ServiceConfig:
    return ServiceConfig(
        tracker=TrackerConfig(
            active_states=active if active is not None else ["Todo", "In Progress"],
            terminal_states=terminal if terminal is not None else ["Done", "Canceled"],
        ),
        workspace=WorkspaceConfig(root=None if no_root else tmp_path),
        hooks=HooksConfig(before_remove=before_remove),
        claude=ClaudeConfig(stall_timeout_ms=stall_timeout_ms),
    )


class _StopSpy:
    """Records which running entries were asked to stop."""

    def __init__(self) -> None:
        self.stopped: list[str] = []

    def __call__(self, entry: RunningEntry) -> None:
        self.stopped.append(entry.run_attempt.issue_id)


class _Refresher:
    """Fake IssueStateRefresher returning fixed issues (or raising)."""

    def __init__(self, issues: list[Issue], *, raises: bool = False) -> None:
        self._issues = issues
        self._raises = raises
        self.calls: list[list[str]] = []

    def fetch_issue_states_by_ids(self, issue_ids: Sequence[str]) -> list[Issue]:
        self.calls.append(list(issue_ids))
        if self._raises:
            raise LinearApiRequestError("refresh boom")
        return list(self._issues)


def _run_entry(
    state: OrchestratorState,
    issue: Issue,
    *,
    started_at: datetime = _FIXED_NOW,
    last_event: datetime | None = None,
    attempt: int | None = None,
    workspace_root: Path | None = None,
) -> None:
    workspace_path = (
        workspace_path_for(issue.identifier, workspace_root)
        if workspace_root is not None
        else Path("/ws") / issue.identifier
    )
    session = LiveSession(session_id="s1", last_event_timestamp=last_event)
    state.running[issue.id] = RunningEntry(
        run_attempt=RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            workspace_path=workspace_path,
            started_at=started_at,
            attempt=attempt,
        ),
        issue=issue,
        session=session if last_event is not None else None,
    )
    state.claimed.add(issue.id)


# --- terminate_running_issue ---------------------------------------------------


def test_terminate_stops_worker_drops_running_and_releases_claim(
    tmp_path: Path,
) -> None:
    state = OrchestratorState()
    _run_entry(state, _issue(state="In Progress"))
    stop = _StopSpy()

    terminate_running_issue(
        state,
        "i-1",
        cleanup_workspace=False,
        config=_service_config(tmp_path),
        stop_worker=stop,
    )

    assert stop.stopped == ["i-1"]
    assert not is_running(state, "i-1")
    assert not is_claimed(state, "i-1")


def test_terminate_cleanup_runs_before_remove_then_removes_workspace(
    tmp_path: Path,
) -> None:
    state = OrchestratorState()
    ws = ensure_workspace("ABC-1", tmp_path).path
    sentinel = tmp_path / "before_remove.ran"
    _run_entry(state, _issue(), workspace_root=tmp_path)

    terminate_running_issue(
        state,
        "i-1",
        cleanup_workspace=True,
        config=_service_config(tmp_path, before_remove=f"touch {sentinel}"),
        stop_worker=_StopSpy(),
    )

    assert sentinel.exists()  # hook ran (while the workspace still existed)
    assert not ws.exists()  # workspace removed afterwards


def test_terminate_cleanup_skipped_when_no_workspace_root(tmp_path: Path) -> None:
    state = OrchestratorState()
    ws = ensure_workspace("ABC-1", tmp_path).path
    _run_entry(state, _issue(), workspace_root=tmp_path)

    terminate_running_issue(
        state,
        "i-1",
        cleanup_workspace=True,
        config=_service_config(tmp_path, no_root=True),
        stop_worker=_StopSpy(),
    )

    assert ws.exists()  # no root configured -> nothing removed
    assert not is_running(state, "i-1")


def test_terminate_is_noop_when_issue_not_running(tmp_path: Path) -> None:
    state = OrchestratorState()
    stop = _StopSpy()

    terminate_running_issue(
        state,
        "missing",
        cleanup_workspace=True,
        config=_service_config(tmp_path),
        stop_worker=stop,
    )

    assert stop.stopped == []


# --- reconcile_stalled_runs (Part A) -------------------------------------------


def _past(ms: int) -> datetime:
    return _FIXED_NOW - timedelta(milliseconds=ms)


def test_stall_terminates_and_retries_when_idle_too_long(tmp_path: Path) -> None:
    state = OrchestratorState()
    _run_entry(state, _issue(state="In Progress"), started_at=_past(60000), attempt=1)
    stop = _StopSpy()
    retry = _SpyRetry()

    reconcile_stalled_runs(
        state,
        config=_service_config(tmp_path, stall_timeout_ms=30000),
        stop_worker=stop,
        schedule_retry=retry,
        now=lambda: _FIXED_NOW,
    )

    assert stop.stopped == ["i-1"]
    assert not is_running(state, "i-1")
    assert retry.calls == [
        {
            "issue_id": "i-1",
            "attempt": 2,
            "identifier": "ABC-1",
            "error": "worker stalled",
        }
    ]


def test_stall_measures_from_last_event_when_present(tmp_path: Path) -> None:
    state = OrchestratorState()
    # Started long ago, but a stream event arrived recently -> not stalled.
    _run_entry(
        state,
        _issue(state="In Progress"),
        started_at=_past(60000),
        last_event=_past(1000),
    )
    retry = _SpyRetry()

    reconcile_stalled_runs(
        state,
        config=_service_config(tmp_path, stall_timeout_ms=30000),
        stop_worker=_StopSpy(),
        schedule_retry=retry,
        now=lambda: _FIXED_NOW,
    )

    assert is_running(state, "i-1")
    assert retry.calls == []


def test_stall_ignores_fresh_workers(tmp_path: Path) -> None:
    state = OrchestratorState()
    _run_entry(state, _issue(state="In Progress"), started_at=_past(5000))
    retry = _SpyRetry()

    reconcile_stalled_runs(
        state,
        config=_service_config(tmp_path, stall_timeout_ms=30000),
        stop_worker=_StopSpy(),
        schedule_retry=retry,
        now=lambda: _FIXED_NOW,
    )

    assert is_running(state, "i-1")
    assert retry.calls == []


def test_stall_detection_disabled_when_timeout_non_positive(tmp_path: Path) -> None:
    state = OrchestratorState()
    _run_entry(state, _issue(state="In Progress"), started_at=_past(10_000_000))
    stop = _StopSpy()

    reconcile_stalled_runs(
        state,
        config=_service_config(tmp_path, stall_timeout_ms=0),
        stop_worker=stop,
        schedule_retry=_SpyRetry(),
        now=lambda: _FIXED_NOW,
    )

    assert is_running(state, "i-1")
    assert stop.stopped == []


# --- reconcile_running_issues (Part B) -----------------------------------------


def _reconcile(
    state: OrchestratorState,
    config: ServiceConfig,
    refresher: _Refresher,
    stop: _StopSpy,
) -> None:
    reconcile_running_issues(
        state,
        config=config,
        tracker=refresher,
        stop_worker=stop,
        schedule_retry=_SpyRetry(),
        now=lambda: _FIXED_NOW,
    )


def test_reconcile_terminal_issue_stops_and_cleans_workspace(tmp_path: Path) -> None:
    state = OrchestratorState()
    ws = ensure_workspace("ABC-1", tmp_path).path
    _run_entry(state, _issue(state="In Progress"), workspace_root=tmp_path)
    stop = _StopSpy()
    refresher = _Refresher([_issue(state="Done")])

    _reconcile(state, _service_config(tmp_path), refresher, stop)

    assert refresher.calls == [["i-1"]]
    assert stop.stopped == ["i-1"]
    assert not is_running(state, "i-1")
    assert not is_claimed(state, "i-1")
    assert not ws.exists()


def test_reconcile_active_issue_refreshes_snapshot(tmp_path: Path) -> None:
    state = OrchestratorState()
    _run_entry(state, _issue(state="In Progress"))
    updated = _issue(state="In Progress", title="Renamed")
    stop = _StopSpy()

    _reconcile(state, _service_config(tmp_path), _Refresher([updated]), stop)

    assert is_running(state, "i-1")
    assert stop.stopped == []
    assert state.running["i-1"].issue is updated


def test_reconcile_non_active_non_terminal_stops_without_cleanup(
    tmp_path: Path,
) -> None:
    state = OrchestratorState()
    ws = ensure_workspace("ABC-1", tmp_path).path
    _run_entry(state, _issue(state="In Progress"), workspace_root=tmp_path)
    stop = _StopSpy()
    # "In Review" is neither active nor terminal in this config.
    refresher = _Refresher([_issue(state="In Review")])

    _reconcile(state, _service_config(tmp_path), refresher, stop)

    assert stop.stopped == ["i-1"]
    assert not is_running(state, "i-1")
    assert not is_claimed(state, "i-1")
    assert ws.exists()  # workspace preserved


def test_reconcile_keeps_workers_running_on_refresh_failure(tmp_path: Path) -> None:
    state = OrchestratorState()
    _run_entry(state, _issue(state="In Progress"))
    stop = _StopSpy()

    _reconcile(state, _service_config(tmp_path), _Refresher([], raises=True), stop)

    assert is_running(state, "i-1")
    assert stop.stopped == []


def test_reconcile_skips_refresh_when_nothing_running(tmp_path: Path) -> None:
    state = OrchestratorState()
    refresher = _Refresher([_issue(state="Done")])

    _reconcile(state, _service_config(tmp_path), refresher, _StopSpy())

    assert refresher.calls == []  # no running ids -> no tracker call


def test_reconcile_ignores_refreshed_issue_not_in_running(tmp_path: Path) -> None:
    state = OrchestratorState()
    _run_entry(state, _issue(state="In Progress"))
    # Tracker echoes an extra id that is not tracked as running (defensive path).
    refresher = _Refresher(
        [
            _issue("i-1", state="In Progress"),
            _issue("ghost", "ZZ-9", state="In Progress"),
        ]
    )

    _reconcile(state, _service_config(tmp_path), refresher, _StopSpy())

    assert is_running(state, "i-1")
    assert not is_running(state, "ghost")
