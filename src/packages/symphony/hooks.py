"""Workspace lifecycle hook execution (SPEC §9.4).

Four optional shell hooks let a workflow prepare and tear down a workspace:
``after_create`` (one-time setup of a new workspace), ``before_run`` (per-attempt
preparation), ``after_run`` (per-attempt cleanup), and ``before_remove`` (teardown).
Each is a shell script string from the ``hooks`` config group, run with the
workspace directory as ``cwd`` under ``sh -lc`` — the POSIX conforming default
(SPEC §9.4) — and bounded by ``hooks.timeout_ms``.

The hooks differ only in how their failures are treated (SPEC §9.4):

* ``after_create`` and ``before_run`` are **fatal** — a non-zero exit, a timeout,
  or a spawn failure raises, aborting workspace creation / the run attempt.
* ``after_run`` and ``before_remove`` are **best-effort** — failures are logged and
  ignored so cleanup never blocks progress.

This module owns hook *execution* and that failure policy. Wiring each hook to its
call site is the orchestrator's job (SPEC §16.5): ``before_run``/``after_run`` are
driven by the worker attempt and ``before_remove`` by cleanup. The one piece of
state-dependent gating handed off by the workspace manager — running
``after_create`` only for a freshly created workspace (SPEC §9.2) — is provided here
as :func:`run_after_create`.
"""

from __future__ import annotations

import logging
import subprocess
from enum import Enum
from pathlib import Path

from symphony.config import HooksConfig
from symphony.exceptions import HookExecutionError, HookTimeoutError
from symphony.models import Workspace

__all__ = ["HookKind", "run_hook", "run_after_create"]

logger = logging.getLogger("symphony.hooks")

# POSIX conforming default (SPEC §9.4). A login shell so hook scripts see the
# operator's normal environment (PATH, toolchain shims). The script is passed as a
# single argv element to ``-c`` — never interpolated into a command line — so there
# is no shell-injection surface here beyond the script the operator already wrote.
_SHELL = ("sh", "-lc")

# Milliseconds per second, for converting ``hooks.timeout_ms`` to the float seconds
# that :func:`subprocess.run` expects.
_MS_PER_S = 1000


class HookKind(Enum):
    """The four workspace lifecycle hooks (SPEC §9.4).

    The value of each member is the corresponding :class:`HooksConfig` attribute
    name, so it doubles as the config lookup key and the log label.
    """

    AFTER_CREATE = "after_create"
    BEFORE_RUN = "before_run"
    AFTER_RUN = "after_run"
    BEFORE_REMOVE = "before_remove"


# Hooks whose failure/timeout aborts the surrounding operation (SPEC §9.4). The
# others are best-effort: their failures are logged and ignored.
_FATAL_HOOKS = frozenset({HookKind.AFTER_CREATE, HookKind.BEFORE_RUN})


def _script_for(kind: HookKind, config: HooksConfig) -> str | None:
    """Return the configured script for ``kind`` (``None`` when unset)."""
    scripts: dict[HookKind, str | None] = {
        HookKind.AFTER_CREATE: config.after_create,
        HookKind.BEFORE_RUN: config.before_run,
        HookKind.AFTER_RUN: config.after_run,
        HookKind.BEFORE_REMOVE: config.before_remove,
    }
    return scripts[kind]


def run_hook(kind: HookKind, config: HooksConfig, workspace_path: Path) -> bool:
    """Run one lifecycle hook with its SPEC §9.4 failure policy.

    Args:
        kind: Which hook to run.
        config: The resolved ``hooks`` config (script strings + ``timeout_ms``).
        workspace_path: The per-issue workspace directory used as the hook ``cwd``.

    Returns:
        ``True`` when the hook ran successfully or was unconfigured (a no-op);
        ``False`` when a **best-effort** hook (``after_run``/``before_remove``)
        failed and the failure was ignored.

    Raises:
        HookExecutionError: A **fatal** hook (``after_create``/``before_run``)
            exited non-zero or could not be started.
        HookTimeoutError: A **fatal** hook exceeded ``hooks.timeout_ms``.
    """
    script = _script_for(kind, config)
    if script is None or not script.strip():
        return True

    fatal = kind in _FATAL_HOOKS
    logger.info("running %s hook (cwd=%s)", kind.value, workspace_path)
    try:
        result = subprocess.run(
            [*_SHELL, script],
            cwd=workspace_path,
            timeout=config.timeout_ms / _MS_PER_S,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _fail(
            kind,
            fatal,
            HookTimeoutError(
                f"{kind.value} hook timed out after {config.timeout_ms} ms"
            ),
            exc,
        )
    except OSError as exc:
        return _fail(
            kind,
            fatal,
            HookExecutionError(f"{kind.value} hook could not be started: {exc}"),
            exc,
        )

    if result.returncode != 0:
        return _fail(
            kind,
            fatal,
            HookExecutionError(
                f"{kind.value} hook exited with status {result.returncode}"
            ),
            None,
        )

    logger.info("%s hook completed", kind.value)
    return True


def _fail(
    kind: HookKind,
    fatal: bool,
    error: HookExecutionError | HookTimeoutError,
    cause: BaseException | None,
) -> bool:
    """Apply the failure policy for ``kind``: raise if fatal, else log and ignore.

    Logs the failure before raising (per ``TECH.md``). Best-effort hooks return
    ``False`` so a caller can tell an ignored failure from a clean run.
    """
    if fatal:
        logger.error("%s hook failed: %s", kind.value, error)
        raise error from cause
    logger.warning("%s hook failed and was ignored: %s", kind.value, error)
    return False


def run_after_create(config: HooksConfig, workspace: Workspace) -> bool:
    """Run ``after_create`` only for a freshly created workspace (SPEC §9.2).

    Centralizes the ``created_now`` gate the workspace manager hands off: the hook
    runs once, when the workspace directory was created during this call, and is
    skipped on reuse. Failure is fatal to workspace creation (SPEC §9.4).

    Args:
        config: The resolved ``hooks`` config.
        workspace: The workspace returned by
            :func:`symphony.workspace.ensure_workspace`.

    Returns:
        ``True`` when the hook ran successfully or was skipped (reuse / unconfigured).

    Raises:
        HookExecutionError: The hook exited non-zero or could not be started.
        HookTimeoutError: The hook exceeded ``hooks.timeout_ms``.
    """
    if not workspace.created_now:
        return True
    return run_hook(HookKind.AFTER_CREATE, config, workspace.path)
