"""Dispatch preflight validation (SPEC §6.3).

A scheduler preflight that checks whether a *resolved* :class:`ServiceConfig` has
what it needs to poll the tracker and launch workers. It is intentionally narrow
— not a full audit of every workflow behavior.

Two call sites are expected (SPEC §6.3):

* **Startup** — call :func:`ensure_dispatchable`; a failure raises
  :class:`DispatchPreflightError` so startup aborts with an operator-visible error.
* **Per dispatch tick** — call :func:`check_dispatch_preflight`; on problems, skip
  dispatch for that tick (keeping reconciliation active) and surface the problems,
  without raising.

The "workflow file can be loaded and parsed" check from §6.3 is enforced upstream
by the loader/resolver, which raise typed errors before a ``ServiceConfig`` exists;
this module assumes resolution already succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass

from symphony.config import ServiceConfig
from symphony.exceptions import DispatchPreflightError
from symphony.normalization import normalize_state

__all__ = ["PreflightProblem", "check_dispatch_preflight", "ensure_dispatchable"]

# Tracker kinds the core supports today (SPEC §6.4).
_SUPPORTED_TRACKER_KINDS = frozenset({"linear"})


@dataclass(frozen=True, slots=True)
class PreflightProblem:
    """A single failed preflight check.

    Attributes:
        code: Stable machine-readable identifier (matches the tracker error
            categories in SPEC §11.4 where applicable).
        message: Human-readable, operator-facing description.
    """

    code: str
    message: str


def check_dispatch_preflight(config: ServiceConfig) -> list[PreflightProblem]:
    """Return all preflight problems for ``config`` (empty when dispatchable).

    Collects *every* problem rather than stopping at the first so an operator
    sees the full picture in one pass (SPEC §6.3).
    """
    problems: list[PreflightProblem] = []
    kind = config.tracker.kind
    normalized_kind = normalize_state(kind) if kind else ""

    # tracker.kind present and supported.
    if not normalized_kind:
        problems.append(
            PreflightProblem("unsupported_tracker_kind", "tracker.kind is not set")
        )
    elif normalized_kind not in _SUPPORTED_TRACKER_KINDS:
        problems.append(
            PreflightProblem(
                "unsupported_tracker_kind",
                f"unsupported tracker.kind: {kind!r}",
            )
        )

    # tracker.api_key present after $ resolution (resolver sets empty -> None).
    if not config.tracker.api_key:
        problems.append(
            PreflightProblem("missing_tracker_api_key", "tracker.api_key is missing")
        )

    # tracker.project_slug required for linear (SPEC §5.3.1).
    if normalized_kind == "linear" and not config.tracker.project_slug:
        problems.append(
            PreflightProblem(
                "missing_tracker_project_slug",
                "tracker.project_slug is required when tracker.kind is 'linear'",
            )
        )

    # claude.command present and non-empty.
    if not config.claude.command.strip():
        problems.append(
            PreflightProblem("missing_claude_command", "claude.command is empty")
        )

    return problems


def ensure_dispatchable(config: ServiceConfig) -> None:
    """Raise :class:`DispatchPreflightError` if ``config`` is not dispatchable.

    Intended for startup validation (SPEC §6.3). Per-tick callers should use
    :func:`check_dispatch_preflight` and skip dispatch instead of raising.
    """
    problems = check_dispatch_preflight(config)
    if not problems:
        return
    detail = "; ".join(f"{problem.code}: {problem.message}" for problem in problems)
    raise DispatchPreflightError(
        f"dispatch preflight failed [{detail}]",
        problem_codes=[problem.code for problem in problems],
    )
