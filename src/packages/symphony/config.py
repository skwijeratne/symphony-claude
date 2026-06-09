"""Typed service-configuration view (SPEC §4.1.3, §5.3, §6.4).

These are the typed containers that the runtime reads instead of poking at the
raw YAML front-matter map. They mirror the front-matter groups in SPEC §5.3 and
carry the constant defaults from the cheat sheet in SPEC §6.4.

This module is structure only: the resolution pipeline that builds a
:class:`ServiceConfig` from a parsed workflow plus environment/path resolution
(``$VAR``/``~`` expansion, ``workspace.root`` default, per-state concurrency
normalization, validation) lands in the config-layer PR. Values that require
that resolution default to ``None`` here and are filled in by the loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "TrackerConfig",
    "PollingConfig",
    "WorkspaceConfig",
    "HooksConfig",
    "AgentConfig",
    "ClaudeConfig",
    "ServiceConfig",
]

# Defaults that are list values (SPEC §6.4); defined as functions so each
# config instance gets its own copy rather than sharing one mutable list.
_DEFAULT_ACTIVE_STATES = ("Todo", "In Progress")
_DEFAULT_TERMINAL_STATES = ("Closed", "Cancelled", "Canceled", "Duplicate", "Done")


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """`tracker` front-matter group (SPEC §5.3.1, §6.4).

    ``api_key`` and ``project_slug`` are resolved/validated by the config loader
    (``$VAR`` indirection, required-field checks) and so default to ``None``.
    """

    kind: str = "linear"
    endpoint: str = "https://api.linear.app/graphql"
    api_key: str | None = None
    project_slug: str | None = None
    required_labels: list[str] = field(default_factory=list)
    active_states: list[str] = field(
        default_factory=lambda: list(_DEFAULT_ACTIVE_STATES)
    )
    terminal_states: list[str] = field(
        default_factory=lambda: list(_DEFAULT_TERMINAL_STATES)
    )


@dataclass(frozen=True, slots=True)
class PollingConfig:
    """`polling` front-matter group (SPEC §5.3.2, §6.4)."""

    interval_ms: int = 30000


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """`workspace` front-matter group (SPEC §5.3.3, §6.4).

    ``root`` defaults to ``<system-temp>/symphony_workspaces`` resolved to an
    absolute path by the config loader, so it is ``None`` until resolved.
    """

    root: Path | None = None


@dataclass(frozen=True, slots=True)
class HooksConfig:
    """`hooks` front-matter group (SPEC §5.3.4, §6.4, §9.4)."""

    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60000


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """`agent` front-matter group (SPEC §5.3.5, §6.4)."""

    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_backoff_ms: int = 300000
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    """`claude` front-matter group (SPEC §5.3.6, §6.4, §10.1)."""

    command: str = "claude"
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    append_system_prompt: str | None = None
    mcp_config: str | None = None
    add_dirs: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    turn_timeout_ms: int = 3600000
    read_timeout_ms: int = 5000
    stall_timeout_ms: int = 300000


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Typed runtime view of the workflow configuration (SPEC §4.1.3).

    Composes the front-matter groups. Dynamic reload replaces the whole
    ``ServiceConfig`` instance rather than mutating it, hence ``frozen``.
    """

    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
