"""Core domain models (SPEC §4.1).

Typed containers for the entities owned by orchestration, prompt rendering, and
observability. The typed configuration view lives in :mod:`symphony.config`.
These are plain data carriers; the behavior that mutates them (dispatch, retry,
reconciliation, token accounting) lands in later PRs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from symphony.normalization import normalize_state, sanitize_workspace_key

__all__ = [
    "BlockerRef",
    "Issue",
    "WorkflowDefinition",
    "Workspace",
    "RunAttemptPhase",
    "RunAttempt",
    "LiveSession",
    "RetryEntry",
    "RunningEntry",
    "AgentTotals",
    "AgentRateLimits",
    "OrchestratorState",
]


@dataclass(slots=True)
class BlockerRef:
    """Reference to an issue that blocks another issue (SPEC §4.1.1).

    Each field is best-effort: the tracker may expose only a subset for a given
    blocker, so all three are optional.

    Attributes:
        id: Stable tracker-internal ID of the blocking issue, if known.
        identifier: Human-readable key of the blocking issue, if known.
        state: Current tracker state name of the blocking issue, if known.
    """

    id: str | None = None
    identifier: str | None = None
    state: str | None = None


@dataclass(slots=True)
class Issue:
    """Normalized issue record (SPEC §4.1.1).

    Used by orchestration, prompt rendering, and observability output. Labels are
    expected to be normalized to lowercase at the tracker boundary (SPEC §11.3);
    derived identifiers are exposed via :attr:`workspace_key` and
    :attr:`normalized_state` per SPEC §4.2.

    Attributes:
        id: Stable tracker-internal ID; used for lookups and internal map keys.
        identifier: Human-readable ticket key (for example ``ABC-123``); used for
            logs and workspace naming.
        title: Issue title.
        state: Current tracker state name.
        description: Issue body, or ``None``.
        priority: Priority integer, or ``None``. Lower numbers are higher
            priority in dispatch sorting.
        branch_name: Tracker-provided branch metadata, or ``None``.
        url: Tracker URL for the issue, or ``None``.
        labels: Labels normalized to lowercase.
        blocked_by: Blocker references for issues that block this one.
        created_at: Creation timestamp, or ``None``.
        updated_at: Last-update timestamp, or ``None``.
    """

    id: str
    identifier: str
    title: str
    state: str
    description: str | None = None
    priority: int | None = None
    branch_name: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[BlockerRef] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def workspace_key(self) -> str:
        """Filesystem-safe workspace directory name for this issue (SPEC §4.2)."""
        return sanitize_workspace_key(self.identifier)

    @property
    def normalized_state(self) -> str:
        """This issue's state lowercased for comparison (SPEC §4.2)."""
        return normalize_state(self.state)


@dataclass(slots=True)
class WorkflowDefinition:
    """Parsed ``WORKFLOW.md`` payload (SPEC §4.1.2).

    Attributes:
        config: The YAML front-matter root object.
        prompt_template: The markdown body after the front matter, trimmed.
    """

    config: dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""


@dataclass(slots=True)
class Workspace:
    """Filesystem workspace assigned to one issue identifier (SPEC §4.1.4).

    Attributes:
        path: Absolute workspace path.
        workspace_key: Sanitized issue identifier (SPEC §4.2).
        created_now: Whether this workspace was just created; gates the
            ``after_create`` hook (SPEC §9.2, §9.4).
    """

    path: Path
    workspace_key: str
    created_now: bool = False


class RunAttemptPhase(StrEnum):
    """Run-attempt lifecycle phase (SPEC §7.2).

    The non-terminal phases describe progress; the terminal phases capture
    distinct outcomes because retry logic and logs differ between them.
    """

    PREPARING_WORKSPACE = "preparing_workspace"
    BUILDING_PROMPT = "building_prompt"
    LAUNCHING_AGENT_PROCESS = "launching_agent_process"
    INITIALIZING_SESSION = "initializing_session"
    STREAMING_TURN = "streaming_turn"
    FINISHING = "finishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STALLED = "stalled"
    CANCELED_BY_RECONCILIATION = "canceled_by_reconciliation"


@dataclass(slots=True)
class RunAttempt:
    """One execution attempt for one issue (SPEC §4.1.5).

    Attributes:
        issue_id: Stable tracker-internal ID of the issue.
        issue_identifier: Human-readable ticket key.
        workspace_path: Absolute path of the workspace used for this attempt.
        started_at: When the attempt started.
        attempt: ``None`` for the first run; ``>=1`` for retries/continuation.
        status: Current lifecycle phase.
        error: Failure detail, when the attempt did not succeed.
    """

    issue_id: str
    issue_identifier: str
    workspace_path: Path
    started_at: datetime
    attempt: int | None = None
    status: RunAttemptPhase = RunAttemptPhase.PREPARING_WORKSPACE
    error: str | None = None


@dataclass(slots=True)
class LiveSession:
    """Agent-session metadata tracked while a worker is running (SPEC §4.1.6).

    Attributes:
        session_id: Claude Code session identifier for this worker run; reused
            via ``--resume`` across continuation turns (SPEC §4.2, §10.7).
        agent_pid: PID of the current ``claude`` subprocess, or ``None`` between
            turns.
        last_event: Type/subtype of the last parsed ``stream-json`` event.
        last_event_timestamp: When the last event was observed.
        last_message: Summarized payload of the last event.
        input_tokens: Aggregate input tokens across this run's turns.
        output_tokens: Aggregate output tokens across this run's turns.
        total_tokens: Aggregate total tokens across this run's turns.
        last_cost_usd: ``total_cost_usd`` from the most recent turn, if present.
        turn_count: Number of coding-agent turns started in this worker run.
    """

    session_id: str
    agent_pid: str | None = None
    last_event: str | None = None
    last_event_timestamp: datetime | None = None
    last_message: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    last_cost_usd: float | None = None
    turn_count: int = 0


@dataclass(slots=True)
class RetryEntry:
    """Scheduled retry state for an issue (SPEC §4.1.7).

    Attributes:
        issue_id: Stable tracker-internal ID of the issue.
        identifier: Best-effort human ID for status surfaces/logs.
        attempt: 1-based retry counter.
        due_at_ms: Monotonic-clock timestamp when the retry is due.
        timer_handle: Runtime-specific timer reference, or ``None`` if unset.
        error: The error that triggered the retry, if any.
    """

    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: int
    timer_handle: object | None = None
    error: str | None = None


@dataclass(slots=True)
class RunningEntry:
    """A live worker tracked in :attr:`OrchestratorState.running` (SPEC §4.1.8).

    Holds the in-flight attempt and its agent session. Worker/task handles are
    added by the orchestrator PRs.

    Attributes:
        run_attempt: The attempt currently executing for the issue.
        session: The live agent session, once one has started.
    """

    run_attempt: RunAttempt
    session: LiveSession | None = None


@dataclass(slots=True)
class AgentTotals:
    """Aggregate agent accounting across all runs (SPEC §4.1.8, §13.5).

    Attributes:
        input_tokens: Total input tokens across all runs.
        output_tokens: Total output tokens across all runs.
        total_tokens: Total tokens across all runs.
        runtime_seconds: Aggregate worker runtime in seconds.
        total_cost_usd: Aggregate reported cost in USD.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    runtime_seconds: float = 0.0
    total_cost_usd: float = 0.0


@dataclass(slots=True)
class AgentRateLimits:
    """Latest rate-limit / API-retry snapshot from agent events (SPEC §4.1.8).

    Attributes:
        is_rate_limited: Whether the most recent signal indicated rate limiting.
        retry_after_ms: Suggested wait before retrying, if reported.
        last_api_error_status: Last API error status code observed, if any.
        updated_at: When this snapshot was last updated.
    """

    is_rate_limited: bool = False
    retry_after_ms: int | None = None
    last_api_error_status: int | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class OrchestratorState:
    """Single authoritative in-memory orchestrator state (SPEC §4.1.8).

    The orchestrator is the only component that mutates this (SPEC §7).

    Attributes:
        poll_interval_ms: Current effective poll interval.
        max_concurrent_agents: Current effective global concurrency limit.
        running: Map of ``issue_id`` to the live worker entry.
        claimed: Issue IDs reserved/running/retrying (dispatch guard).
        retry_attempts: Map of ``issue_id`` to its scheduled retry.
        completed: Issue IDs completed; bookkeeping only, not dispatch gating.
        agent_totals: Aggregate tokens, runtime, and cost.
        agent_rate_limits: Latest rate-limit / API-retry snapshot.
    """

    poll_interval_ms: int = 30000
    max_concurrent_agents: int = 10
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    agent_totals: AgentTotals = field(default_factory=AgentTotals)
    agent_rate_limits: AgentRateLimits = field(default_factory=AgentRateLimits)
