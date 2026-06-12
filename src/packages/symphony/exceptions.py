"""Exception hierarchy for Symphony.

Every Symphony error derives from :class:`SymphonyError` and carries a stable,
machine-readable :attr:`SymphonyError.code`. Those codes mirror the error
surfaces and normalized categories defined in ``SPEC.md``:

* Workflow / config error surface (SPEC §5.5)
* Failure classes (SPEC §14.1)
* Agent runner error mapping (SPEC §10.6)
* Tracker error-handling contract (SPEC §11.4)

The hierarchy groups leaf exceptions under one base class per failure class so
callers can catch a whole category (for example :class:`TrackerError`) or a
single condition (for example :class:`MissingWorkflowFileError`). This module is
structure only; raising and handling these errors is wired up in later PRs.
"""

from __future__ import annotations

__all__ = [
    "SymphonyError",
    # Workflow / config (SPEC §5.5, §14.1)
    "WorkflowConfigError",
    "MissingWorkflowFileError",
    "WorkflowParseError",
    "WorkflowFrontMatterNotAMapError",
    "ConfigValidationError",
    "DispatchPreflightError",
    "TemplateParseError",
    "TemplateRenderError",
    # Workspace (SPEC §14.1)
    "WorkspaceError",
    "WorkspaceCreationError",
    "WorkspacePopulationError",
    "InvalidWorkspacePathError",
    "HookExecutionError",
    "HookTimeoutError",
    # Agent session (SPEC §10.6)
    "AgentSessionError",
    "ClaudeNotFoundError",
    "InvalidWorkspaceCwdError",
    "StartupTimeoutError",
    "TurnTimeoutError",
    "ProcessExitError",
    "TurnFailedError",
    "MaxTurnsError",
    "MaxBudgetError",
    "AgentApiError",
    "MalformedOutputError",
    # Tracker (SPEC §11.4)
    "TrackerError",
    "UnsupportedTrackerKindError",
    "MissingTrackerApiKeyError",
    "MissingTrackerProjectSlugError",
    "LinearApiRequestError",
    "LinearApiStatusError",
    "LinearGraphQLError",
    "LinearUnknownPayloadError",
    "LinearMissingEndCursorError",
    # Observability (SPEC §14.1)
    "ObservabilityError",
    "SnapshotTimeoutError",
    "SnapshotUnavailableError",
    "DashboardRenderError",
    "LogSinkConfigError",
]


class SymphonyError(Exception):
    """Base class for every error raised by Symphony.

    Attributes:
        code: Stable, machine-readable identifier for the error condition. Leaf
            classes override this with a value drawn from ``SPEC.md``. It is the
            value used in structured logs and status surfaces, so it is part of
            the observable contract and should not change casually.
        message: Human-readable description. Defaults to :attr:`code` when no
            message is supplied at construction time.
    """

    code: str = "symphony_error"

    def __init__(self, message: str | None = None) -> None:
        """Initialize the error.

        Args:
            message: Optional human-readable description. When omitted, the
                class-level :attr:`code` is used so the error is never empty.
        """
        resolved = message if message is not None else self.code
        super().__init__(resolved)
        self.message = resolved


# ---------------------------------------------------------------------------
# Workflow / config failures (SPEC §5.5, §14.1)
# ---------------------------------------------------------------------------
class WorkflowConfigError(SymphonyError):
    """A workflow file or configuration failure (SPEC §14.1, class 1)."""

    code = "workflow_config_error"


class MissingWorkflowFileError(WorkflowConfigError):
    """The resolved ``WORKFLOW.md`` path does not exist (SPEC §5.5)."""

    code = "missing_workflow_file"


class WorkflowParseError(WorkflowConfigError):
    """The workflow front matter could not be parsed as YAML (SPEC §5.5)."""

    code = "workflow_parse_error"


class WorkflowFrontMatterNotAMapError(WorkflowConfigError):
    """The parsed front matter root is not a mapping object (SPEC §5.5)."""

    code = "workflow_front_matter_not_a_map"


class ConfigValidationError(WorkflowConfigError):
    """A typed config value failed coercion/validation (SPEC §6.1 step 5).

    Raised when a present front-matter value has the wrong type or an
    out-of-range value (for example a non-positive ``agent.max_turns`` or a
    non-integer ``hooks.timeout_ms``; SPEC §5.3.4, §5.3.5).
    """

    code = "config_validation_error"


class DispatchPreflightError(WorkflowConfigError):
    """Dispatch preflight validation failed (SPEC §6.3).

    Raised at startup when the resolved config is not dispatchable. Per-tick
    callers should instead inspect the problems and skip dispatch rather than
    raise. Carries the stable :attr:`problem_codes` of every failed check.
    """

    code = "dispatch_preflight_failed"

    def __init__(
        self, message: str | None = None, *, problem_codes: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.problem_codes = list(problem_codes or [])


class TemplateParseError(WorkflowConfigError):
    """The prompt template failed to compile (SPEC §5.5)."""

    code = "template_parse_error"


class TemplateRenderError(WorkflowConfigError):
    """Rendering failed on an unknown variable, filter, or interpolation.

    See SPEC §5.5. Template errors fail only the affected run attempt rather
    than blocking new dispatches.
    """

    code = "template_render_error"


# ---------------------------------------------------------------------------
# Workspace failures (SPEC §14.1, class 2)
# ---------------------------------------------------------------------------
class WorkspaceError(SymphonyError):
    """A workspace lifecycle failure (SPEC §14.1, class 2)."""

    code = "workspace_error"


class WorkspaceCreationError(WorkspaceError):
    """The workspace directory could not be created (SPEC §14.1)."""

    code = "workspace_creation_failed"


class WorkspacePopulationError(WorkspaceError):
    """Workspace population/synchronization failed (SPEC §14.1)."""

    code = "workspace_population_failed"


class InvalidWorkspacePathError(WorkspaceError):
    """The configured workspace path is invalid (SPEC §14.1, §9.5)."""

    code = "invalid_workspace_path"


class HookExecutionError(WorkspaceError):
    """A workspace lifecycle hook exited with a failure (SPEC §9.4)."""

    code = "hook_failed"


class HookTimeoutError(WorkspaceError):
    """A workspace lifecycle hook exceeded ``hooks.timeout_ms`` (SPEC §9.4)."""

    code = "hook_timeout"


# ---------------------------------------------------------------------------
# Agent session failures (SPEC §10.6, §14.1 class 3)
# ---------------------------------------------------------------------------
class AgentSessionError(SymphonyError):
    """A coding-agent session failure (SPEC §14.1, class 3)."""

    code = "agent_session_error"


class ClaudeNotFoundError(AgentSessionError):
    """The ``claude`` command was not found or is not executable (SPEC §10.6)."""

    code = "claude_not_found"


class InvalidWorkspaceCwdError(AgentSessionError):
    """The workspace cwd for the subprocess is invalid (SPEC §10.6)."""

    code = "invalid_workspace_cwd"


class StartupTimeoutError(AgentSessionError):
    """No ``system``/``init`` event arrived within ``read_timeout_ms``.

    See SPEC §10.6.
    """

    code = "startup_timeout"


class TurnTimeoutError(AgentSessionError):
    """A turn exceeded ``turn_timeout_ms`` wall-clock (SPEC §10.6)."""

    code = "turn_timeout"


class ProcessExitError(AgentSessionError):
    """The subprocess exited non-zero with no successful ``result``.

    See SPEC §10.6.
    """

    code = "process_exit"


class TurnFailedError(AgentSessionError):
    """The terminal ``result`` reported an error subtype or ``is_error``.

    See SPEC §10.6.
    """

    code = "turn_failed"


class MaxTurnsError(AgentSessionError):
    """The run terminated with ``result`` subtype ``error_max_turns``.

    See SPEC §10.6.
    """

    code = "max_turns"


class MaxBudgetError(AgentSessionError):
    """The run terminated with ``result`` subtype ``error_max_budget_usd``.

    See SPEC §10.6.
    """

    code = "max_budget"


class AgentApiError(AgentSessionError):
    """A terminating API error (``result.api_error_status`` set) (SPEC §10.6)."""

    code = "api_error"


class MalformedOutputError(AgentSessionError):
    """The subprocess produced unparseable ``stream-json`` output (SPEC §10.6)."""

    code = "malformed_output"


# ---------------------------------------------------------------------------
# Tracker failures (SPEC §11.4, §14.1 class 4)
# ---------------------------------------------------------------------------
class TrackerError(SymphonyError):
    """An issue-tracker integration failure (SPEC §14.1, class 4)."""

    code = "tracker_error"


class UnsupportedTrackerKindError(TrackerError):
    """The configured tracker kind is not supported (SPEC §11.4)."""

    code = "unsupported_tracker_kind"


class MissingTrackerApiKeyError(TrackerError):
    """No tracker API key was resolved from configuration (SPEC §11.4)."""

    code = "missing_tracker_api_key"


class MissingTrackerProjectSlugError(TrackerError):
    """No tracker project slug was resolved from configuration (SPEC §11.4)."""

    code = "missing_tracker_project_slug"


class LinearApiRequestError(TrackerError):
    """A transport-level failure talking to the Linear API (SPEC §11.4)."""

    code = "linear_api_request"


class LinearApiStatusError(TrackerError):
    """The Linear API returned a non-200 HTTP status (SPEC §11.4)."""

    code = "linear_api_status"


class LinearGraphQLError(TrackerError):
    """The Linear response carried GraphQL ``errors`` (SPEC §11.4)."""

    code = "linear_graphql_errors"


class LinearUnknownPayloadError(TrackerError):
    """The Linear response payload had an unexpected shape (SPEC §11.4)."""

    code = "linear_unknown_payload"


class LinearMissingEndCursorError(TrackerError):
    """Pagination integrity error: an expected end cursor was absent.

    See SPEC §11.4.
    """

    code = "linear_missing_end_cursor"


# ---------------------------------------------------------------------------
# Observability failures (SPEC §14.1, class 5)
# ---------------------------------------------------------------------------
class ObservabilityError(SymphonyError):
    """An observability/monitoring failure (SPEC §14.1, class 5)."""

    code = "observability_error"


class SnapshotTimeoutError(ObservabilityError):
    """A runtime snapshot request exceeded its timeout (SPEC §13.3)."""

    code = "snapshot_timeout"


class SnapshotUnavailableError(ObservabilityError):
    """The orchestrator state is not available for a snapshot (SPEC §13.3)."""

    code = "snapshot_unavailable"


class DashboardRenderError(ObservabilityError):
    """The human-readable dashboard failed to render (SPEC §14.1)."""

    code = "dashboard_render_error"


class LogSinkConfigError(ObservabilityError):
    """A log sink could not be configured (SPEC §14.1)."""

    code = "log_sink_config_error"
