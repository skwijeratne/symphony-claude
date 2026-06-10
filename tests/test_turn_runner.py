"""Tests for the single-turn lifecycle runner (SPEC §10.2, §10.6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from symphony.config import ClaudeConfig
from symphony.exceptions import (
    AgentApiError,
    InvalidWorkspaceCwdError,
    MaxBudgetError,
    MaxTurnsError,
    ProcessExitError,
    StartupTimeoutError,
    TurnFailedError,
    TurnTimeoutError,
)
from symphony.stream_parser import AgentEvent, AgentEventType
from symphony.turn_runner import run_turn


def _emit(**event: Any) -> str:
    """A bash line that prints one stream-json event."""
    return "echo '" + json.dumps(event) + "'\n"


def _config(
    tmp_path: Path,
    body: str,
    *,
    read_timeout_ms: int = 3000,
    turn_timeout_ms: int = 8000,
) -> ClaudeConfig:
    """A ClaudeConfig whose command is a fake claude emitting ``body``."""
    script = tmp_path / "fake-claude"
    script.write_text("#!/usr/bin/env bash\n" + body)
    script.chmod(0o755)
    return ClaudeConfig(
        command=str(script),
        read_timeout_ms=read_timeout_ms,
        turn_timeout_ms=turn_timeout_ms,
    )


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


_INIT = {"type": "system", "subtype": "init", "session_id": "s1"}
_OK_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "s1",
    "num_turns": 2,
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


# --- success path (§10.3, §4.1.6) ---------------------------------------------
def test_successful_turn_outcome_and_accounting(tmp_path: Path) -> None:
    body = _emit(**_INIT) + _emit(type="assistant") + _emit(**_OK_RESULT) + "exit 0\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert result.succeeded
    assert result.error is None
    assert result.session_id == "s1"
    assert result.exit_code == 0
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (
        10,
        5,
        15,
    )
    assert result.cost_usd == 0.0123
    assert result.num_turns == 2


def test_explicit_total_tokens_is_used(tmp_path: Path) -> None:
    result_event = {**_OK_RESULT, "usage": {"input_tokens": 3, "total_tokens": 99}}
    body = _emit(**_INIT) + _emit(**result_event) + "exit 0\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert result.total_tokens == 99


def test_missing_usage_defaults_to_zero(tmp_path: Path) -> None:
    body = _emit(**_INIT) + _emit(type="result", subtype="success", is_error=False)
    body += "exit 0\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert result.succeeded
    assert result.total_tokens == 0 and result.cost_usd is None


def test_events_are_forwarded(tmp_path: Path) -> None:
    body = _emit(**_INIT) + _emit(type="assistant") + _emit(**_OK_RESULT) + "exit 0\n"
    seen: list[AgentEvent] = []
    run_turn(
        _config(tmp_path, body),
        workspace_path=_workspace(tmp_path),
        prompt="go",
        on_event=seen.append,
    )
    assert [e.type for e in seen] == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.NOTIFICATION,
        AgentEventType.TURN_COMPLETED,
    ]


def test_session_id_captured_when_not_passed(tmp_path: Path) -> None:
    body = _emit(**_INIT) + _emit(**_OK_RESULT) + "exit 0\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert result.session_id == "s1"


# --- failure mapping (§10.6) --------------------------------------------------
def test_error_max_turns(tmp_path: Path) -> None:
    body = _emit(**_INIT) + _emit(
        type="result", subtype="error_max_turns", is_error=True
    )
    body += "exit 1\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert isinstance(result.error, MaxTurnsError)
    assert not result.succeeded


def test_error_max_budget(tmp_path: Path) -> None:
    body = _emit(**_INIT)
    body += _emit(type="result", subtype="error_max_budget_usd", is_error=True)
    body += "exit 1\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert isinstance(result.error, MaxBudgetError)


def test_api_error(tmp_path: Path) -> None:
    body = _emit(**_INIT)
    body += _emit(
        type="result",
        subtype="error_during_execution",
        is_error=True,
        api_error_status=529,
    )
    body += "exit 1\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert isinstance(result.error, AgentApiError)


def test_generic_turn_failed(tmp_path: Path) -> None:
    body = _emit(**_INIT)
    body += _emit(type="result", subtype="error_during_execution", is_error=True)
    body += "exit 1\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert isinstance(result.error, TurnFailedError)


def test_success_result_but_nonzero_exit_is_process_exit(tmp_path: Path) -> None:
    body = _emit(**_INIT) + _emit(**_OK_RESULT) + "exit 3\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert isinstance(result.error, ProcessExitError)


def test_no_result_event_is_process_exit_with_stderr(tmp_path: Path) -> None:
    body = _emit(**_INIT) + "echo 'boom on stderr' >&2\n" + "exit 2\n"
    result = run_turn(
        _config(tmp_path, body), workspace_path=_workspace(tmp_path), prompt="go"
    )
    assert isinstance(result.error, ProcessExitError)
    assert "boom on stderr" in str(result.error)


def test_invalid_workspace_is_returned_as_error(tmp_path: Path) -> None:
    body = _emit(**_OK_RESULT)
    result = run_turn(
        _config(tmp_path, body), workspace_path=tmp_path / "missing", prompt="go"
    )
    assert isinstance(result.error, InvalidWorkspaceCwdError)
    assert result.exit_code is None


# --- timeouts (§10.6) ---------------------------------------------------------
def test_startup_timeout_when_no_init(tmp_path: Path) -> None:
    # Sleeps well past read_timeout before any output -> startup_timeout.
    body = "sleep 5\n" + _emit(**_INIT)
    config = _config(tmp_path, body, read_timeout_ms=200, turn_timeout_ms=8000)
    result = run_turn(config, workspace_path=_workspace(tmp_path), prompt="go")
    assert isinstance(result.error, StartupTimeoutError)


def test_turn_timeout_when_no_result(tmp_path: Path) -> None:
    # init arrives, then the turn hangs past turn_timeout with no result.
    body = _emit(**_INIT) + "sleep 5\n"
    config = _config(tmp_path, body, read_timeout_ms=3000, turn_timeout_ms=400)
    result = run_turn(config, workspace_path=_workspace(tmp_path), prompt="go")
    assert isinstance(result.error, TurnTimeoutError)
