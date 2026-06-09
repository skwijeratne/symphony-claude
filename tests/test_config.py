"""Tests for the typed config view defaults/construction (SPEC §4.1.3, §6.4)."""

from __future__ import annotations

from pathlib import Path

from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    HooksConfig,
    PollingConfig,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)


def test_tracker_defaults():
    cfg = TrackerConfig()
    assert cfg.kind == "linear"
    assert cfg.endpoint == "https://api.linear.app/graphql"
    assert cfg.api_key is None
    assert cfg.project_slug is None
    assert cfg.required_labels == []
    assert cfg.active_states == ["Todo", "In Progress"]
    assert cfg.terminal_states == [
        "Closed",
        "Cancelled",
        "Canceled",
        "Duplicate",
        "Done",
    ]


def test_tracker_state_defaults_are_independent():
    a = TrackerConfig()
    b = TrackerConfig()
    a.active_states.append("Backlog")
    assert b.active_states == ["Todo", "In Progress"]


def test_polling_and_hooks_defaults():
    assert PollingConfig().interval_ms == 30000
    hooks = HooksConfig()
    assert hooks.after_create is None
    assert hooks.before_run is None
    assert hooks.after_run is None
    assert hooks.before_remove is None
    assert hooks.timeout_ms == 60000


def test_workspace_root_unresolved_by_default():
    assert WorkspaceConfig().root is None
    assert WorkspaceConfig(root=Path("/tmp/ws")).root == Path("/tmp/ws")


def test_agent_defaults():
    cfg = AgentConfig()
    assert cfg.max_concurrent_agents == 10
    assert cfg.max_turns == 20
    assert cfg.max_retry_backoff_ms == 300000
    assert cfg.max_concurrent_agents_by_state == {}


def test_claude_defaults():
    cfg = ClaudeConfig()
    assert cfg.command == "claude"
    assert cfg.model is None
    assert cfg.permission_mode is None
    assert cfg.allowed_tools == []
    assert cfg.disallowed_tools == []
    assert cfg.add_dirs == []
    assert cfg.extra_args == []
    assert cfg.turn_timeout_ms == 3600000
    assert cfg.read_timeout_ms == 5000
    assert cfg.stall_timeout_ms == 300000


def test_service_config_composes_group_defaults():
    cfg = ServiceConfig()
    assert isinstance(cfg.tracker, TrackerConfig)
    assert isinstance(cfg.polling, PollingConfig)
    assert isinstance(cfg.workspace, WorkspaceConfig)
    assert isinstance(cfg.hooks, HooksConfig)
    assert isinstance(cfg.agent, AgentConfig)
    assert isinstance(cfg.claude, ClaudeConfig)
    assert cfg.polling.interval_ms == 30000


def test_service_config_instances_do_not_share_groups():
    a = ServiceConfig()
    b = ServiceConfig()
    assert a.tracker is not b.tracker
    a.claude.allowed_tools.append("Bash")
    assert b.claude.allowed_tools == []
