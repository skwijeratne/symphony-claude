"""Tests for the config resolver (SPEC §6.1, §5.3, §17.1 config)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from symphony.config import ServiceConfig
from symphony.config_resolver import resolve_config
from symphony.exceptions import ConfigValidationError
from symphony.models import WorkflowDefinition


def _resolve(config, *, workflow_dir, env=None) -> ServiceConfig:
    definition = WorkflowDefinition(config=config, prompt_template="")
    return resolve_config(definition, workflow_dir=workflow_dir, env=env or {})


# --- defaults (SPEC §6.4, §17.1) ----------------------------------------------


def test_empty_config_applies_all_defaults(tmp_path):
    cfg = _resolve({}, workflow_dir=tmp_path)
    assert cfg.tracker.kind == "linear"
    assert cfg.tracker.endpoint == "https://api.linear.app/graphql"
    assert cfg.tracker.api_key is None
    assert cfg.tracker.project_slug is None
    assert cfg.tracker.required_labels == []
    assert cfg.tracker.active_states == ["Todo", "In Progress"]
    assert cfg.polling.interval_ms == 30000
    assert cfg.hooks.timeout_ms == 60000
    assert cfg.agent.max_concurrent_agents == 10
    assert cfg.agent.max_turns == 20
    assert cfg.agent.max_concurrent_agents_by_state == {}
    assert cfg.claude.command == "claude"
    assert cfg.claude.turn_timeout_ms == 3600000


def test_workspace_root_default_is_system_temp(tmp_path):
    cfg = _resolve({}, workflow_dir=tmp_path)
    assert cfg.workspace.root == Path(tempfile.gettempdir()) / "symphony_workspaces"


# --- $VAR / api_key (SPEC §5.3.1, §17.1) --------------------------------------


def test_api_key_literal_is_used_verbatim(tmp_path):
    cfg = _resolve({"tracker": {"api_key": "lin_abc"}}, workflow_dir=tmp_path)
    assert cfg.tracker.api_key == "lin_abc"


def test_api_key_var_indirection(tmp_path):
    cfg = _resolve(
        {"tracker": {"api_key": "$LINEAR_API_KEY"}},
        workflow_dir=tmp_path,
        env={"LINEAR_API_KEY": "lin_secret"},
    )
    assert cfg.tracker.api_key == "lin_secret"


def test_api_key_var_resolving_to_empty_is_missing(tmp_path):
    cfg = _resolve(
        {"tracker": {"api_key": "$LINEAR_API_KEY"}},
        workflow_dir=tmp_path,
        env={"LINEAR_API_KEY": ""},
    )
    assert cfg.tracker.api_key is None


def test_api_key_var_unset_is_missing(tmp_path):
    cfg = _resolve({"tracker": {"api_key": "$NOPE"}}, workflow_dir=tmp_path, env={})
    assert cfg.tracker.api_key is None


# --- workspace.root paths (SPEC §5.3.3, §6.1, §17.1) --------------------------


def test_workspace_root_absolute_is_preserved(tmp_path):
    target = tmp_path / "ws"
    cfg = _resolve({"workspace": {"root": str(target)}}, workflow_dir=tmp_path)
    assert cfg.workspace.root == target


def test_workspace_root_relative_resolves_against_workflow_dir(tmp_path):
    cfg = _resolve({"workspace": {"root": "ws/sub"}}, workflow_dir=tmp_path)
    assert cfg.workspace.root == tmp_path / "ws" / "sub"
    assert cfg.workspace.root.is_absolute()


def test_workspace_root_tilde_expansion(tmp_path):
    cfg = _resolve({"workspace": {"root": "~/sym_ws"}}, workflow_dir=tmp_path)
    assert cfg.workspace.root == Path.home() / "sym_ws"


def test_workspace_root_var_expansion(tmp_path):
    cfg = _resolve(
        {"workspace": {"root": "$WS_ROOT/sub"}},
        workflow_dir=tmp_path,
        env={"WS_ROOT": str(tmp_path / "base")},
    )
    assert cfg.workspace.root == tmp_path / "base" / "sub"


# --- claude.command preserved (SPEC §5.3.6, §17.1) ----------------------------


def test_claude_command_preserved_verbatim(tmp_path):
    # No $VAR/path expansion is applied to the shell command string.
    command = "claude --foo $NOT_EXPANDED"
    cfg = _resolve(
        {"claude": {"command": command}},
        workflow_dir=tmp_path,
        env={"NOT_EXPANDED": "should-not-appear"},
    )
    assert cfg.claude.command == command


# --- per-state concurrency normalization (SPEC §5.3.5, §17.1) -----------------


def test_per_state_concurrency_normalizes_and_ignores_invalid(tmp_path):
    cfg = _resolve(
        {
            "agent": {
                "max_concurrent_agents_by_state": {
                    "In Progress": 3,
                    "TODO": 1,
                    "Done": 0,  # non-positive -> ignored
                    "Backlog": -2,  # negative -> ignored
                    "Review": "lots",  # non-numeric -> ignored
                    "Flag": True,  # bool -> ignored
                }
            }
        },
        workflow_dir=tmp_path,
    )
    assert cfg.agent.max_concurrent_agents_by_state == {"in progress": 3, "todo": 1}


def test_per_state_concurrency_non_map_ignored(tmp_path):
    cfg = _resolve(
        {"agent": {"max_concurrent_agents_by_state": "oops"}}, workflow_dir=tmp_path
    )
    assert cfg.agent.max_concurrent_agents_by_state == {}


# --- typed coercion happy path ------------------------------------------------


def test_typed_values_are_read(tmp_path):
    cfg = _resolve(
        {
            "polling": {"interval_ms": 5000},
            "agent": {"max_turns": 7},
            "claude": {
                "allowed_tools": ["Read", "Edit"],
                "read_timeout_ms": 1000,
            },
        },
        workflow_dir=tmp_path,
    )
    assert cfg.polling.interval_ms == 5000
    assert cfg.agent.max_turns == 7
    assert cfg.claude.allowed_tools == ["Read", "Edit"]
    assert cfg.claude.read_timeout_ms == 1000


# --- validation (SPEC §5.3.4, §5.3.5, §6.1 step 5) ----------------------------


@pytest.mark.parametrize(
    "config",
    [
        {"polling": {"interval_ms": "soon"}},  # non-integer
        {"polling": {"interval_ms": True}},  # bool rejected
        {"agent": {"max_turns": 0}},  # not positive
        {"agent": {"max_turns": -1}},  # not positive
        {"hooks": {"timeout_ms": 1.5}},  # float not int
        {"tracker": {"required_labels": "bug"}},  # not a list
        {"tracker": {"active_states": [1, 2]}},  # list of non-strings
        {"tracker": {"kind": 5}},  # non-string
        {"claude": {"command": 42}},  # non-string
        {"tracker": "not-a-map"},  # group not a map
        {"workspace": {"root": 5}},  # non-string path
    ],
)
def test_invalid_values_raise_config_validation_error(tmp_path, config):
    with pytest.raises(ConfigValidationError) as exc:
        _resolve(config, workflow_dir=tmp_path)
    assert exc.value.code == "config_validation_error"


def test_env_defaults_to_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "from-os-environ")
    definition = WorkflowDefinition(
        config={"tracker": {"api_key": "$LINEAR_API_KEY"}}, prompt_template=""
    )
    cfg = resolve_config(definition, workflow_dir=tmp_path)
    assert cfg.tracker.api_key == "from-os-environ"
    assert isinstance(os.environ, type(os.environ))  # sanity: real env was used
