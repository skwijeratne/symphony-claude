"""Agent Runner: one worker attempt over back-to-back turns (SPEC §10.7, §16.5).

A worker attempt for one issue: prepare the workspace, run the first turn with the
full rendered prompt, then keep running continuation turns on the same live session
(``--resume``) until the issue leaves an active state or ``agent.max_turns`` is
reached — re-checking the tracker between turns (SPEC §7.1, §16.5). The orchestrator
(M5) decides whether to schedule another worker session afterward; this module owns
one session only.

Turn/prompt rules:

* Turn 1 uses the full rendered task prompt (SPEC §12). Continuation turns send only
  continuation guidance and ``--resume <session_id>`` — the session history is
  already on disk, so the original prompt is not resent (SPEC §7.1, §10.2). The
  ``session_id`` is captured from the first turn and reused for every continuation.

Hook/outcome rules (SPEC §9.4, §16.5):

* ``after_create`` runs for a freshly created workspace (fatal on failure);
  ``before_run`` runs once per attempt (fatal); ``after_run`` runs best-effort on
  the way out (success or failure).
* Any failure — workspace, fatal hook, prompt render, a failed turn, or a tracker
  refresh error — ends the attempt with that error in :attr:`AttemptResult.error`.
  Token/cost usage is aggregated across the turns that did run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from symphony.config import ServiceConfig
from symphony.exceptions import (
    InvalidWorkspacePathError,
    SymphonyError,
    TemplateParseError,
    TemplateRenderError,
    TrackerError,
    WorkspaceError,
)
from symphony.hooks import HookKind, run_after_create, run_hook
from symphony.models import Issue
from symphony.normalization import normalize_state
from symphony.prompt_renderer import render_prompt
from symphony.stream_parser import AgentEvent
from symphony.turn_runner import TurnResult, run_turn
from symphony.workspace import ensure_workspace

__all__ = [
    "DEFAULT_CONTINUATION_PROMPT",
    "AttemptResult",
    "IssueStateRefresher",
    "build_turn_prompt",
    "run_agent_attempt",
]

# Continuation turns send guidance only, never the original prompt (SPEC §7.1).
DEFAULT_CONTINUATION_PROMPT = (
    "Continue working on this issue from where the previous turn left off. "
    "Review the current state of the workspace, proceed with the remaining work, "
    "and stop once the issue has reached its next handoff state."
)

# The single-turn runner signature, injectable so the loop is testable without
# spawning real subprocesses.
RunTurn = Callable[..., TurnResult]


class IssueStateRefresher(Protocol):
    """The tracker capability the runner needs between turns (SPEC §16.5)."""

    def fetch_issue_states_by_ids(self, issue_ids: Sequence[str]) -> list[Issue]: ...


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """Outcome and aggregate accounting for one worker attempt (SPEC §16.5).

    Attributes:
        error: The failure that ended the attempt, or ``None`` on success.
        session_id: The session id captured for this worker run, if any.
        turns: Number of coding-agent turns that ran.
        input_tokens / output_tokens / total_tokens: Summed across the turns.
        cost_usd: Summed ``total_cost_usd`` across the turns, or ``None`` if none
            was reported.
        final_state: The issue's tracker state observed last, when known.
    """

    error: SymphonyError | None
    session_id: str | None
    turns: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    final_state: str | None

    @property
    def succeeded(self) -> bool:
        """Whether the attempt completed without error."""
        return self.error is None


def build_turn_prompt(
    template: str,
    issue: Issue,
    attempt: int | None,
    turn_number: int,
    continuation_prompt: str,
) -> str:
    """Build the prompt for a turn (SPEC §7.1, §16.5).

    The first turn renders the full task template (SPEC §12); later turns send only
    continuation guidance, since the session already holds the original prompt.

    Raises:
        TemplateParseError, TemplateRenderError: The first-turn template failed to
            render (SPEC §5.5).
    """
    if turn_number == 1:
        return render_prompt(template, issue, attempt)
    return continuation_prompt


def run_agent_attempt(
    issue: Issue,
    *,
    config: ServiceConfig,
    prompt_template: str,
    tracker: IssueStateRefresher,
    attempt: int | None = None,
    on_event: Callable[[AgentEvent], None] | None = None,
    continuation_prompt: str = DEFAULT_CONTINUATION_PROMPT,
    run_turn_fn: RunTurn = run_turn,
) -> AttemptResult:
    """Run one worker attempt for ``issue`` (SPEC §10.7, §16.5).

    Args:
        issue: The issue to work.
        config: The resolved service configuration.
        prompt_template: The workflow prompt body (rendered for the first turn).
        tracker: Tracker client used to refresh issue state between turns.
        attempt: Worker-attempt number (``None`` first run, ``>=1`` on retry).
        on_event: Optional per-event callback forwarded to each turn.
        continuation_prompt: Guidance sent on continuation turns.
        run_turn_fn: The single-turn runner (injectable for tests).

    Returns:
        The :class:`AttemptResult`; a failed attempt carries the error rather than
        raising.
    """
    accounting = _Accounting()

    root = config.workspace.root
    if root is None:
        return accounting.failed(
            InvalidWorkspacePathError("workspace.root is not configured")
        )

    # Workspace + after_create (fatal on failure); no after_run has run yet.
    try:
        workspace = ensure_workspace(issue.identifier, root)
        run_after_create(config.hooks, workspace)
    except WorkspaceError as exc:
        return accounting.failed(exc)

    # before_run is fatal to the attempt (SPEC §9.4); after_run does not run here.
    try:
        run_hook(HookKind.BEFORE_RUN, config.hooks, workspace.path)
    except WorkspaceError as exc:
        return accounting.failed(exc)

    active_states = {normalize_state(state) for state in config.tracker.active_states}
    max_turns = config.agent.max_turns
    current_issue = issue
    session_id: str | None = None
    turn_number = 1

    while True:
        try:
            prompt = build_turn_prompt(
                prompt_template,
                current_issue,
                attempt,
                turn_number,
                continuation_prompt,
            )
        except (TemplateParseError, TemplateRenderError) as exc:
            return accounting.finish_with(exc, config, workspace.path, session_id)

        turn = run_turn_fn(
            config.claude,
            workspace_path=workspace.path,
            prompt=prompt,
            resume_session_id=session_id if turn_number > 1 else None,
            on_event=on_event,
        )
        accounting.add_turn(turn)
        session_id = session_id or turn.session_id

        if turn.error is not None:
            return accounting.finish_with(
                turn.error, config, workspace.path, session_id
            )

        try:
            refreshed = tracker.fetch_issue_states_by_ids([current_issue.id])
        except TrackerError as exc:
            return accounting.finish_with(exc, config, workspace.path, session_id)
        if refreshed:
            current_issue = refreshed[0]

        if current_issue.normalized_state not in active_states:
            break
        if turn_number >= max_turns or session_id is None:
            break
        turn_number += 1

    return accounting.finish_with(
        None, config, workspace.path, session_id, final_state=current_issue.state
    )


@dataclass(slots=True)
class _Accounting:
    """Accumulates per-turn token/cost usage and builds the final result."""

    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None

    def add_turn(self, turn: TurnResult) -> None:
        self.turns += 1
        self.input_tokens += turn.input_tokens
        self.output_tokens += turn.output_tokens
        self.total_tokens += turn.total_tokens
        if turn.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + turn.cost_usd

    def failed(self, error: SymphonyError) -> AttemptResult:
        """Result for a failure before any turn ran (no after_run to run)."""
        return self._result(error, session_id=None, final_state=None)

    def finish_with(
        self,
        error: SymphonyError | None,
        config: ServiceConfig,
        workspace_path: Path,
        session_id: str | None,
        *,
        final_state: str | None = None,
    ) -> AttemptResult:
        """Run the best-effort after_run hook, then build the result (SPEC §9.4)."""
        run_hook(HookKind.AFTER_RUN, config.hooks, workspace_path)
        return self._result(error, session_id=session_id, final_state=final_state)

    def _result(
        self,
        error: SymphonyError | None,
        *,
        session_id: str | None,
        final_state: str | None,
    ) -> AttemptResult:
        return AttemptResult(
            error=error,
            session_id=session_id,
            turns=self.turns,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            cost_usd=self.cost_usd,
            final_state=final_state,
        )
