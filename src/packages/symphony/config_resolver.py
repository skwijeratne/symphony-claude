"""Resolve a raw workflow front-matter map into a typed :class:`ServiceConfig`.

This is step 3-5 of the configuration resolution pipeline (SPEC §6.1): apply
built-in defaults for missing optional fields, resolve ``$VAR`` indirection and
``~`` for env-backed/path values, normalize ``workspace.root`` to an absolute
path, normalize the per-state concurrency map, and coerce/validate typed values.

It deliberately does *not* perform dispatch preflight validation (tracker kind
support, api_key/project_slug presence; SPEC §6.3) — that is a separate scheduler
concern handled by a later PR. Here, a *present* value with the wrong type or an
out-of-range value raises :class:`ConfigValidationError`; *missing* values fall
back to defaults.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from symphony.config import (
    AgentConfig,
    ClaudeConfig,
    HooksConfig,
    PollingConfig,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from symphony.exceptions import ConfigValidationError
from symphony.models import WorkflowDefinition
from symphony.normalization import normalize_state

__all__ = ["resolve_config"]

_DEFAULT_ENDPOINT = "https://api.linear.app/graphql"
_DEFAULT_ACTIVE_STATES = ["Todo", "In Progress"]
_DEFAULT_TERMINAL_STATES = ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
_WORKSPACE_DIRNAME = "symphony_workspaces"

# Matches $VAR and ${VAR} for env expansion against the injected env mapping
# (os.path.expandvars only consults the real os.environ).
_VAR_PATTERN = re.compile(r"\$(\w+)|\$\{([^}]*)\}")


def _expandvars(value: str, env: Mapping[str, str]) -> str:
    """Expand ``$VAR``/``${VAR}`` using ``env``; leave unknown names unchanged."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or ""
        return env.get(name, match.group(0))

    return _VAR_PATTERN.sub(replace, value)


# --- typed getters ------------------------------------------------------------
def _group(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return the front-matter sub-map ``name``, or empty if absent/null."""
    raw = config.get(name)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigValidationError(f"'{name}' must be a map, got {type(raw).__name__}")
    return raw


def _get_int(
    group: Mapping[str, Any], key: str, default: int, *, positive: bool = False
) -> int:
    if key not in group:
        return default
    raw = group[key]
    # bool is an int subclass; reject it as a misconfiguration.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigValidationError(
            f"'{key}' must be an integer, got {type(raw).__name__}"
        )
    if positive and raw <= 0:
        raise ConfigValidationError(f"'{key}' must be a positive integer, got {raw}")
    return raw


def _get_str(group: Mapping[str, Any], key: str, default: str) -> str:
    if key not in group:
        return default
    raw = group[key]
    if not isinstance(raw, str):
        raise ConfigValidationError(
            f"'{key}' must be a string, got {type(raw).__name__}"
        )
    return raw


def _get_opt_str(group: Mapping[str, Any], key: str) -> str | None:
    raw = group.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigValidationError(
            f"'{key}' must be a string, got {type(raw).__name__}"
        )
    return raw


def _get_str_list(group: Mapping[str, Any], key: str, default: list[str]) -> list[str]:
    if key not in group:
        return list(default)
    raw = group[key]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigValidationError(f"'{key}' must be a list of strings")
    return list(raw)


# --- value coercion (SPEC §6.1) ----------------------------------------------
def _resolve_api_key(group: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    """Resolve ``tracker.api_key`` (SPEC §5.3.1).

    A leading ``$`` names an environment variable; a value resolving to the
    empty string is treated as missing (``None``).
    """
    raw = group.get("api_key")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigValidationError(
            f"'api_key' must be a string, got {type(raw).__name__}"
        )
    resolved = env.get(raw[1:], "") if raw.startswith("$") else raw
    return resolved or None


def _to_absolute(value: str, base: Path, env: Mapping[str, str]) -> Path:
    """Expand ``~``/``$VAR`` and resolve to an absolute path (SPEC §5.3.3, §6.1)."""
    path = Path(_expandvars(value, env)).expanduser()
    if not path.is_absolute():
        path = base / path
    # os.path.abspath normalizes (collapses '..') and anchors relative paths to
    # cwd WITHOUT resolving symlinks; Path.resolve() would resolve symlinks, which
    # makes resolved roots unpredictable. Keep the predictable lexical form.
    return Path(os.path.abspath(path))  # noqa: PTH100


def _normalize_state_concurrency(raw: Any) -> dict[str, int]:
    """Normalize ``agent.max_concurrent_agents_by_state`` (SPEC §5.3.5).

    State keys are lowercased; entries whose value is not a positive integer are
    ignored rather than rejected.
    """
    result: dict[str, int] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        result[normalize_state(str(key))] = value
    return result


# --- group resolvers ----------------------------------------------------------
def _resolve_tracker(group: Mapping[str, Any], env: Mapping[str, str]) -> TrackerConfig:
    return TrackerConfig(
        kind=_get_str(group, "kind", "linear"),
        endpoint=_get_str(group, "endpoint", _DEFAULT_ENDPOINT),
        api_key=_resolve_api_key(group, env),
        project_slug=_get_opt_str(group, "project_slug"),
        required_labels=_get_str_list(group, "required_labels", []),
        active_states=_get_str_list(group, "active_states", _DEFAULT_ACTIVE_STATES),
        terminal_states=_get_str_list(
            group, "terminal_states", _DEFAULT_TERMINAL_STATES
        ),
    )


def _resolve_workspace(
    group: Mapping[str, Any], workflow_dir: Path, env: Mapping[str, str]
) -> WorkspaceConfig:
    raw = group.get("root")
    if raw is None:
        root = Path(tempfile.gettempdir()) / _WORKSPACE_DIRNAME
    elif isinstance(raw, str):
        root = _to_absolute(raw, workflow_dir, env)
    else:
        raise ConfigValidationError(
            f"'root' must be a string path, got {type(raw).__name__}"
        )
    return WorkspaceConfig(root=root)


def _resolve_hooks(group: Mapping[str, Any]) -> HooksConfig:
    return HooksConfig(
        after_create=_get_opt_str(group, "after_create"),
        before_run=_get_opt_str(group, "before_run"),
        after_run=_get_opt_str(group, "after_run"),
        before_remove=_get_opt_str(group, "before_remove"),
        timeout_ms=_get_int(group, "timeout_ms", 60000),
    )


def _resolve_agent(group: Mapping[str, Any]) -> AgentConfig:
    return AgentConfig(
        max_concurrent_agents=_get_int(group, "max_concurrent_agents", 10),
        max_turns=_get_int(group, "max_turns", 20, positive=True),
        max_retry_backoff_ms=_get_int(group, "max_retry_backoff_ms", 300000),
        max_concurrent_agents_by_state=_normalize_state_concurrency(
            group.get("max_concurrent_agents_by_state", {})
        ),
    )


def _resolve_claude(group: Mapping[str, Any]) -> ClaudeConfig:
    return ClaudeConfig(
        # command is preserved verbatim as a shell command string (no expansion).
        command=_get_str(group, "command", "claude"),
        model=_get_opt_str(group, "model"),
        permission_mode=_get_opt_str(group, "permission_mode"),
        allowed_tools=_get_str_list(group, "allowed_tools", []),
        disallowed_tools=_get_str_list(group, "disallowed_tools", []),
        append_system_prompt=_get_opt_str(group, "append_system_prompt"),
        mcp_config=_get_opt_str(group, "mcp_config"),
        add_dirs=_get_str_list(group, "add_dirs", []),
        extra_args=_get_str_list(group, "extra_args", []),
        turn_timeout_ms=_get_int(group, "turn_timeout_ms", 3600000),
        read_timeout_ms=_get_int(group, "read_timeout_ms", 5000),
        stall_timeout_ms=_get_int(group, "stall_timeout_ms", 300000),
    )


def resolve_config(
    definition: WorkflowDefinition,
    *,
    workflow_dir: Path,
    env: Mapping[str, str] | None = None,
) -> ServiceConfig:
    """Build a typed :class:`ServiceConfig` from a parsed workflow (SPEC §6.1).

    Args:
        definition: The parsed workflow (front-matter map + prompt body).
        workflow_dir: Directory containing the selected ``WORKFLOW.md``; relative
            ``workspace.root`` values resolve against it (SPEC §6.1).
        env: Environment mapping for ``$VAR`` resolution; defaults to
            ``os.environ``.

    Returns:
        The fully resolved configuration.

    Raises:
        ConfigValidationError: A present value has the wrong type or is out of
            range.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    config = definition.config
    return ServiceConfig(
        tracker=_resolve_tracker(_group(config, "tracker"), environ),
        polling=PollingConfig(
            interval_ms=_get_int(_group(config, "polling"), "interval_ms", 30000)
        ),
        workspace=_resolve_workspace(
            _group(config, "workspace"), workflow_dir, environ
        ),
        hooks=_resolve_hooks(_group(config, "hooks")),
        agent=_resolve_agent(_group(config, "agent")),
        claude=_resolve_claude(_group(config, "claude")),
    )
