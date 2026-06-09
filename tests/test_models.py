"""Tests for the core domain models (SPEC §4.1, §4.2)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from symphony.models import (
    AgentRateLimits,
    AgentTotals,
    BlockerRef,
    Issue,
    LiveSession,
    OrchestratorState,
    RetryEntry,
    RunAttempt,
    RunAttemptPhase,
    RunningEntry,
    WorkflowDefinition,
    Workspace,
)


def _issue(**overrides: Any) -> Issue:
    base: dict[str, Any] = {
        "id": "iss_1",
        "identifier": "ABC-123",
        "title": "Title",
        "state": "Todo",
    }
    base.update(overrides)
    return Issue(**base)


def test_issue_defaults():
    issue = _issue()
    assert issue.description is None
    assert issue.priority is None
    assert issue.branch_name is None
    assert issue.url is None
    assert issue.labels == []
    assert issue.blocked_by == []
    assert issue.created_at is None
    assert issue.updated_at is None


def test_issue_default_collections_are_independent():
    a = _issue()
    b = _issue()
    a.labels.append("bug")
    a.blocked_by.append(BlockerRef(id="x"))
    assert b.labels == []
    assert b.blocked_by == []


def test_issue_full_construction():
    created = datetime(2026, 1, 1)
    updated = datetime(2026, 1, 2)
    blocker = BlockerRef(id="iss_0", identifier="ABC-1", state="In Progress")
    issue = _issue(
        description="body",
        priority=2,
        branch_name="feature/abc-123",
        url="https://tracker/ABC-123",
        labels=["bug"],
        blocked_by=[blocker],
        created_at=created,
        updated_at=updated,
    )
    assert issue.priority == 2
    assert issue.blocked_by == [blocker]
    assert issue.created_at == created
    assert issue.updated_at == updated


def test_workspace_key_derives_from_identifier():
    assert _issue(identifier="feature/login").workspace_key == "feature_login"
    assert _issue(identifier="ABC-123").workspace_key == "ABC-123"


def test_normalized_state_lowercases():
    assert _issue(state="In Progress").normalized_state == "in progress"


def test_blocker_ref_defaults():
    ref = BlockerRef()
    assert ref.id is None
    assert ref.identifier is None
    assert ref.state is None


def test_workflow_definition_defaults_and_construction():
    assert WorkflowDefinition().config == {}
    assert WorkflowDefinition().prompt_template == ""
    wd = WorkflowDefinition(config={"tracker": {"kind": "linear"}}, prompt_template="x")
    assert wd.config["tracker"]["kind"] == "linear"
    assert wd.prompt_template == "x"


def test_workflow_definition_default_config_is_independent():
    a = WorkflowDefinition()
    b = WorkflowDefinition()
    a.config["k"] = 1
    assert b.config == {}


def test_workspace_defaults():
    ws = Workspace(path=Path("/ws/ABC-123"), workspace_key="ABC-123")
    assert ws.path == Path("/ws/ABC-123")
    assert ws.workspace_key == "ABC-123"
    assert ws.created_now is False


def test_run_attempt_defaults():
    started = datetime(2026, 1, 1)
    attempt = RunAttempt(
        issue_id="iss_1",
        issue_identifier="ABC-123",
        workspace_path=Path("/ws/ABC-123"),
        started_at=started,
    )
    assert attempt.attempt is None  # None => first run
    assert attempt.status is RunAttemptPhase.PREPARING_WORKSPACE
    assert attempt.error is None


def test_run_attempt_phase_is_str_enum():
    assert RunAttemptPhase.SUCCEEDED == "succeeded"
    assert RunAttemptPhase("failed") is RunAttemptPhase.FAILED
    # All 11 lifecycle phases from SPEC §7.2 are present.
    assert len(list(RunAttemptPhase)) == 11


def test_live_session_defaults():
    session = LiveSession(session_id="sess-1")
    assert session.agent_pid is None
    assert session.last_event is None
    assert session.input_tokens == 0
    assert session.output_tokens == 0
    assert session.total_tokens == 0
    assert session.last_cost_usd is None
    assert session.turn_count == 0


def test_retry_entry_defaults():
    entry = RetryEntry(
        issue_id="iss_1", identifier="ABC-123", attempt=1, due_at_ms=1234
    )
    assert entry.timer_handle is None
    assert entry.error is None


def test_running_entry_holds_attempt_and_optional_session():
    attempt = RunAttempt(
        issue_id="iss_1",
        issue_identifier="ABC-123",
        workspace_path=Path("/ws/ABC-123"),
        started_at=datetime(2026, 1, 1),
    )
    entry = RunningEntry(run_attempt=attempt)
    assert entry.session is None
    entry2 = RunningEntry(run_attempt=attempt, session=LiveSession(session_id="s"))
    assert entry2.session is not None


def test_agent_totals_and_rate_limits_defaults():
    totals = AgentTotals()
    assert totals.total_tokens == 0
    assert totals.runtime_seconds == 0.0
    assert totals.total_cost_usd == 0.0
    limits = AgentRateLimits()
    assert limits.is_rate_limited is False
    assert limits.retry_after_ms is None
    assert limits.last_api_error_status is None
    assert limits.updated_at is None


def test_orchestrator_state_defaults():
    state = OrchestratorState()
    assert state.poll_interval_ms == 30000
    assert state.max_concurrent_agents == 10
    assert state.running == {}
    assert state.claimed == set()
    assert state.retry_attempts == {}
    assert state.completed == set()
    assert isinstance(state.agent_totals, AgentTotals)
    assert isinstance(state.agent_rate_limits, AgentRateLimits)


def test_orchestrator_state_collections_are_independent():
    a = OrchestratorState()
    b = OrchestratorState()
    a.claimed.add("iss_1")
    a.running["iss_1"] = RunningEntry(
        run_attempt=RunAttempt(
            issue_id="iss_1",
            issue_identifier="ABC-123",
            workspace_path=Path("/ws"),
            started_at=datetime(2026, 1, 1),
        )
    )
    assert b.claimed == set()
    assert b.running == {}
