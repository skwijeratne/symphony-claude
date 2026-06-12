"""Tests for the runtime snapshot interface (SPEC §13.3, §13.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from symphony.exceptions import SnapshotTimeoutError, SnapshotUnavailableError
from symphony.models import (
    AgentRateLimits,
    AgentTotals,
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunAttempt,
    RunningEntry,
)
from symphony.snapshot import (
    RuntimeSnapshot,
    SnapshotProvider,
    build_snapshot,
)

_FIXED_NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
_FIXED_NOW_MS = 1_000_000


def _now() -> datetime:
    return _FIXED_NOW


def _now_ms() -> int:
    return _FIXED_NOW_MS


def _running_entry(
    issue_id: str,
    identifier: str,
    *,
    started_seconds_ago: float,
    session: LiveSession | None = None,
    url: str | None = None,
) -> RunningEntry:
    return RunningEntry(
        run_attempt=RunAttempt(
            issue_id=issue_id,
            issue_identifier=identifier,
            workspace_path=Path("/ws") / identifier,
            started_at=_FIXED_NOW - timedelta(seconds=started_seconds_ago),
            attempt=2,
        ),
        issue=Issue(
            id=issue_id,
            identifier=identifier,
            title="t",
            state="In Progress",
            url=url,
        ),
        session=session,
    )


# --- snapshot shape (SPEC §13.3) -------------------------------------------------
def test_running_rows_carry_identity_url_turns_and_elapsed() -> None:
    state = OrchestratorState()
    session = LiveSession(session_id="sess-1", turn_count=3, total_tokens=42)
    state.running["i-1"] = _running_entry(
        "i-1",
        "ABC-1",
        started_seconds_ago=90.0,
        session=session,
        url="https://linear.app/t/ABC-1",
    )

    snapshot = build_snapshot(state, now=_now, now_ms=_now_ms)

    assert len(snapshot.running) == 1
    row = snapshot.running[0]
    assert row.issue_id == "i-1"
    assert row.issue_identifier == "ABC-1"
    assert row.issue_url == "https://linear.app/t/ABC-1"
    assert row.attempt == 2
    assert row.seconds_running == pytest.approx(90.0)
    assert row.session_id == "sess-1"
    assert row.turn_count == 3
    assert row.total_tokens == 42
    assert snapshot.taken_at == _FIXED_NOW


def test_running_row_defaults_before_a_session_or_issue_exists() -> None:
    state = OrchestratorState()
    entry = _running_entry("i-1", "ABC-1", started_seconds_ago=5.0)
    entry.issue = None
    state.running["i-1"] = entry

    row = build_snapshot(state, now=_now, now_ms=_now_ms).running[0]

    assert row.issue_url is None
    assert row.session_id is None
    assert row.turn_count == 0
    assert row.total_tokens == 0


def test_rows_are_sorted_by_issue_identifier() -> None:
    state = OrchestratorState()
    state.running["i-2"] = _running_entry("i-2", "ABC-2", started_seconds_ago=1.0)
    state.running["i-1"] = _running_entry("i-1", "ABC-1", started_seconds_ago=1.0)
    state.retry_attempts["i-4"] = RetryEntry("i-4", "ABC-4", 1, _FIXED_NOW_MS)
    state.retry_attempts["i-3"] = RetryEntry("i-3", "ABC-3", 1, _FIXED_NOW_MS)

    snapshot = build_snapshot(state, now=_now, now_ms=_now_ms)

    assert [r.issue_identifier for r in snapshot.running] == ["ABC-1", "ABC-2"]
    assert [r.issue_identifier for r in snapshot.retrying] == ["ABC-3", "ABC-4"]


def test_retry_rows_expose_relative_due_time_floored_at_zero() -> None:
    state = OrchestratorState()
    state.retry_attempts["i-1"] = RetryEntry(
        "i-1", "ABC-1", 3, _FIXED_NOW_MS + 4500, error="worker stalled"
    )
    state.retry_attempts["i-2"] = RetryEntry("i-2", "ABC-2", 1, _FIXED_NOW_MS - 100)

    rows = build_snapshot(state, now=_now, now_ms=_now_ms).retrying

    assert rows[0].issue_id == "i-1"
    assert rows[0].attempt == 3
    assert rows[0].due_in_ms == 4500
    assert rows[0].error == "worker stalled"
    assert rows[1].due_in_ms == 0  # overdue is floored, never negative


def test_totals_include_active_session_elapsed_seconds() -> None:
    # SPEC §13.5: seconds_running = cumulative ended-session runtime plus each
    # active session's elapsed time, derived at snapshot time.
    state = OrchestratorState(
        agent_totals=AgentTotals(
            input_tokens=10,
            output_tokens=20,
            total_tokens=30,
            runtime_seconds=100.0,
            total_cost_usd=1.25,
        )
    )
    state.running["i-1"] = _running_entry("i-1", "ABC-1", started_seconds_ago=60.0)
    state.running["i-2"] = _running_entry("i-2", "ABC-2", started_seconds_ago=30.0)

    totals = build_snapshot(state, now=_now, now_ms=_now_ms).agent_totals

    assert totals.input_tokens == 10
    assert totals.output_tokens == 20
    assert totals.total_tokens == 30
    assert totals.cost_usd == 1.25
    assert totals.seconds_running == pytest.approx(190.0)


def test_rate_limits_are_copied_not_shared() -> None:
    state = OrchestratorState(
        agent_rate_limits=AgentRateLimits(is_rate_limited=True, retry_after_ms=5000)
    )

    snapshot = build_snapshot(state, now=_now, now_ms=_now_ms)
    state.agent_rate_limits.is_rate_limited = False

    assert snapshot.rate_limits.is_rate_limited is True
    assert snapshot.rate_limits.retry_after_ms == 5000


def test_snapshot_does_not_mutate_state() -> None:
    state = OrchestratorState()
    state.running["i-1"] = _running_entry("i-1", "ABC-1", started_seconds_ago=1.0)
    state.retry_attempts["i-2"] = RetryEntry("i-2", "ABC-2", 1, _FIXED_NOW_MS)

    build_snapshot(state, now=_now, now_ms=_now_ms)

    assert set(state.running) == {"i-1"}
    assert set(state.retry_attempts) == {"i-2"}
    assert state.agent_totals.runtime_seconds == 0.0


# --- SnapshotProvider error modes (SPEC §13.3) ------------------------------------
def test_provider_builds_a_snapshot_with_the_configured_timeout() -> None:
    state = OrchestratorState()
    seen_timeouts: list[float] = []

    def request_state(timeout_s: float) -> OrchestratorState:
        seen_timeouts.append(timeout_s)
        return state

    provider = SnapshotProvider(request_state, timeout_ms=500, now=_now, now_ms=_now_ms)

    assert isinstance(provider.take(), RuntimeSnapshot)
    assert seen_timeouts == [0.5]


def test_provider_maps_timeout_to_snapshot_timeout_error() -> None:
    def request_state(timeout_s: float) -> OrchestratorState:
        raise TimeoutError

    provider = SnapshotProvider(request_state, timeout_ms=250)

    with pytest.raises(SnapshotTimeoutError):
        provider.take()


def test_provider_maps_missing_state_to_unavailable_error() -> None:
    provider = SnapshotProvider(lambda timeout_s: None)

    with pytest.raises(SnapshotUnavailableError):
        provider.take()
