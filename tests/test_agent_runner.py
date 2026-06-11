"""Tests for the Agent Runner worker attempt (SPEC §10.7, §16.5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from symphony.agent_runner import (
    DEFAULT_CONTINUATION_PROMPT,
    AttemptResult,
    run_agent_attempt,
)
from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    HooksConfig,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from symphony.exceptions import (
    HookExecutionError,
    InvalidWorkspacePathError,
    TemplateRenderError,
    TrackerError,
    TurnFailedError,
)
from symphony.models import Issue
from symphony.turn_runner import TurnResult

_TEMPLATE = "Work on {{ issue.identifier }}: {{ issue.title }}."


def _issue(state: str = "In Progress") -> Issue:
    return Issue(id="i-1", identifier="ABC-1", title="Fix bug", state=state)


def _config(
    tmp_path: Path, *, hooks: HooksConfig | None = None, max_turns: int = 5
) -> ServiceConfig:
    return ServiceConfig(
        tracker=TrackerConfig(active_states=["Todo", "In Progress"]),
        workspace=WorkspaceConfig(root=tmp_path / "ws-root"),
        hooks=hooks or HooksConfig(),
        agent=AgentConfig(max_turns=max_turns),
        claude=ClaudeConfig(),
    )


def _turn(
    *,
    error: Any = None,
    session_id: str | None = "s1",
    tokens: int = 10,
    cost: float | None = 0.01,
) -> TurnResult:
    return TurnResult(
        error=error,
        session_id=session_id,
        result_event=None,
        exit_code=0,
        input_tokens=tokens,
        output_tokens=tokens,
        total_tokens=tokens * 2,
        cost_usd=cost,
        num_turns=1,
    )


class FakeTracker:
    """Returns a queued issue on each refresh, recording the ids asked for."""

    def __init__(self, states: list[str]) -> None:
        self._states = list(states)
        self.calls: list[list[str]] = []

    def fetch_issue_states_by_ids(self, issue_ids: Any) -> list[Issue]:
        self.calls.append(list(issue_ids))
        state = self._states.pop(0) if self._states else "Done"
        return [Issue(id="i-1", identifier="ABC-1", title="Fix bug", state=state)]


class RecordingRunTurn:
    """A run_turn stand-in returning queued TurnResults and recording its calls."""

    def __init__(self, results: list[TurnResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, config: Any, **kwargs: Any) -> TurnResult:
        self.calls.append(kwargs)
        return self._results.pop(0)


# --- single turn, issue leaves active state (§16.5) ---------------------------
def test_single_turn_completes_when_issue_becomes_terminal(tmp_path: Path) -> None:
    tracker = FakeTracker(["Done"])
    run_turn_fn = RecordingRunTurn([_turn()])
    result = run_agent_attempt(
        _issue(),
        config=_config(tmp_path),
        prompt_template=_TEMPLATE,
        tracker=tracker,
        run_turn_fn=run_turn_fn,
    )
    assert isinstance(result, AttemptResult)
    assert result.succeeded
    assert result.turns == 1
    assert result.session_id == "s1"
    assert result.final_state == "Done"
    # First turn renders the full template and starts a fresh session.
    assert run_turn_fn.calls[0]["prompt"] == "Work on ABC-1: Fix bug."
    assert run_turn_fn.calls[0]["resume_session_id"] is None


# --- continuation while active, session reuse (§7.1, §10.2) -------------------
def test_continues_on_same_session_until_terminal(tmp_path: Path) -> None:
    tracker = FakeTracker(["In Progress", "Done"])
    run_turn_fn = RecordingRunTurn([_turn(), _turn()])
    result = run_agent_attempt(
        _issue(),
        config=_config(tmp_path),
        prompt_template=_TEMPLATE,
        tracker=tracker,
        run_turn_fn=run_turn_fn,
    )
    assert result.succeeded and result.turns == 2
    # Continuation turn resumes the captured session with guidance, not the template.
    assert run_turn_fn.calls[1]["resume_session_id"] == "s1"
    assert run_turn_fn.calls[1]["prompt"] == DEFAULT_CONTINUATION_PROMPT
    # Aggregated accounting across both turns.
    assert result.total_tokens == 40
    assert result.cost_usd == 0.02


def test_stops_at_max_turns_while_still_active(tmp_path: Path) -> None:
    tracker = FakeTracker(["In Progress", "In Progress", "In Progress"])
    run_turn_fn = RecordingRunTurn([_turn(), _turn()])
    result = run_agent_attempt(
        _issue(),
        config=_config(tmp_path, max_turns=2),
        prompt_template=_TEMPLATE,
        tracker=tracker,
        run_turn_fn=run_turn_fn,
    )
    assert result.succeeded
    assert result.turns == 2  # capped by max_turns even though still active


# --- failures (§16.5) ---------------------------------------------------------
def test_turn_failure_ends_attempt(tmp_path: Path) -> None:
    tracker = FakeTracker(["In Progress"])
    failing = _turn(error=TurnFailedError("boom"))
    run_turn_fn = RecordingRunTurn([failing])
    result = run_agent_attempt(
        _issue(),
        config=_config(tmp_path),
        prompt_template=_TEMPLATE,
        tracker=tracker,
        run_turn_fn=run_turn_fn,
    )
    assert not result.succeeded
    assert isinstance(result.error, TurnFailedError)
    assert result.turns == 1
    # A failed turn is not followed by a tracker refresh.
    assert tracker.calls == []


def test_tracker_refresh_failure_ends_attempt(tmp_path: Path) -> None:
    class BoomTracker:
        def fetch_issue_states_by_ids(self, issue_ids: Any) -> list[Issue]:
            raise TrackerError("tracker down")

    run_turn_fn = RecordingRunTurn([_turn()])
    result = run_agent_attempt(
        _issue(),
        config=_config(tmp_path),
        prompt_template=_TEMPLATE,
        tracker=BoomTracker(),
        run_turn_fn=run_turn_fn,
    )
    assert isinstance(result.error, TrackerError)
    assert result.turns == 1


def test_prompt_render_failure_ends_attempt(tmp_path: Path) -> None:
    tracker = FakeTracker(["In Progress"])
    run_turn_fn = RecordingRunTurn([])
    result = run_agent_attempt(
        _issue(),
        config=_config(tmp_path),
        prompt_template="{{ unknown_var }}",
        tracker=tracker,
        run_turn_fn=run_turn_fn,
    )
    assert isinstance(result.error, TemplateRenderError)
    assert result.turns == 0
    assert run_turn_fn.calls == []  # never launched a turn


def test_before_run_hook_failure_ends_attempt(tmp_path: Path) -> None:
    config = _config(tmp_path, hooks=HooksConfig(before_run="exit 1"))
    run_turn_fn = RecordingRunTurn([])
    result = run_agent_attempt(
        _issue(),
        config=config,
        prompt_template=_TEMPLATE,
        tracker=FakeTracker([]),
        run_turn_fn=run_turn_fn,
    )
    assert isinstance(result.error, HookExecutionError)
    assert result.turns == 0


# --- workspace / hooks wiring -------------------------------------------------
def test_workspace_created_and_after_create_runs(tmp_path: Path) -> None:
    config = _config(tmp_path, hooks=HooksConfig(after_create="touch created.marker"))
    run_turn_fn = RecordingRunTurn([_turn()])
    run_agent_attempt(
        _issue(),
        config=config,
        prompt_template=_TEMPLATE,
        tracker=FakeTracker(["Done"]),
        run_turn_fn=run_turn_fn,
    )
    workspace = tmp_path / "ws-root" / "ABC-1"
    assert workspace.is_dir()
    assert (workspace / "created.marker").exists()
    # run_turn was launched with the workspace as its cwd.
    assert run_turn_fn.calls[0]["workspace_path"] == workspace


def test_after_create_failure_ends_attempt(tmp_path: Path) -> None:
    # after_create failure is fatal to workspace creation (SPEC §9.4); no turn runs.
    config = _config(tmp_path, hooks=HooksConfig(after_create="exit 1"))
    run_turn_fn = RecordingRunTurn([])
    result = run_agent_attempt(
        _issue(),
        config=config,
        prompt_template=_TEMPLATE,
        tracker=FakeTracker([]),
        run_turn_fn=run_turn_fn,
    )
    assert isinstance(result.error, HookExecutionError)
    assert result.turns == 0


def test_unconfigured_workspace_root_fails(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config = ServiceConfig(
        tracker=config.tracker,
        workspace=WorkspaceConfig(root=None),
        hooks=config.hooks,
        agent=config.agent,
        claude=config.claude,
    )
    result = run_agent_attempt(
        _issue(),
        config=config,
        prompt_template=_TEMPLATE,
        tracker=FakeTracker([]),
        run_turn_fn=RecordingRunTurn([]),
    )
    assert isinstance(result.error, InvalidWorkspacePathError)
    assert result.turns == 0


# --- end to end through the real run_turn (fake claude subprocess) ------------
def test_end_to_end_with_real_subprocess(tmp_path: Path) -> None:
    init = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
    done = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s1"}
    )
    fake = tmp_path / "fake-claude"
    fake.write_text(f"#!/usr/bin/env bash\necho '{init}'\necho '{done}'\nexit 0\n")
    fake.chmod(0o755)
    config = ServiceConfig(
        tracker=TrackerConfig(active_states=["Todo", "In Progress"]),
        workspace=WorkspaceConfig(root=tmp_path / "ws-root"),
        hooks=HooksConfig(),
        agent=AgentConfig(max_turns=3),
        claude=ClaudeConfig(
            command=str(fake), read_timeout_ms=3000, turn_timeout_ms=8000
        ),
    )
    result = run_agent_attempt(
        _issue(),
        config=config,
        prompt_template=_TEMPLATE,
        tracker=FakeTracker(["Done"]),
    )
    assert result.succeeded
    assert result.turns == 1
    assert result.session_id == "s1"
    assert result.final_state == "Done"
