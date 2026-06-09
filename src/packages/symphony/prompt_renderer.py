"""Strict per-issue prompt rendering (SPEC §5.4, §12).

Renders the ``WORKFLOW.md`` prompt body (the template) for one issue/attempt using
a strict Liquid engine: unknown variables and unknown filters MUST fail rendering
(SPEC §5.4). The issue is exposed as nested dicts/lists so templates can read
fields and iterate labels/blockers (SPEC §12.2).

Error mapping (SPEC §5.5): template syntax errors become ``template_parse_error``;
everything else raised while rendering (undefined variable, unknown filter, bad
interpolation) becomes ``template_render_error``. Rendering failures fail only the
affected run attempt (SPEC §12.4), which the orchestrator handles like any worker
failure.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from liquid import Environment, StrictUndefined
from liquid.exceptions import LiquidError, LiquidSyntaxError

from symphony.exceptions import TemplateParseError, TemplateRenderError
from symphony.models import Issue

__all__ = ["DEFAULT_PROMPT", "build_issue_context", "render_prompt"]

# Minimal fallback used when the workflow prompt body is empty (SPEC §5.4).
DEFAULT_PROMPT = "You are working on an issue from Linear."

# A single strict environment: undefined variables raise on use, and unknown
# filters raise as well (Liquid's default).
_ENVIRONMENT = Environment(undefined=StrictUndefined)


def build_issue_context(issue: Issue) -> dict[str, Any]:
    """Build the ``issue`` template variable from a normalized issue (SPEC §12.2).

    Nested dataclasses (blockers) and lists (labels) are converted to plain
    dicts/lists so templates can iterate them. The §4.2 derived values
    ``workspace_key`` and ``normalized_state`` are included for convenience.
    """
    context = asdict(issue)
    context["workspace_key"] = issue.workspace_key
    context["normalized_state"] = issue.normalized_state
    return context


def render_prompt(
    template_source: str, issue: Issue, attempt: int | None = None
) -> str:
    """Render the prompt template for one issue/attempt (SPEC §5.4, §12).

    Args:
        template_source: The workflow prompt body. If empty/whitespace, the
            :data:`DEFAULT_PROMPT` fallback is used (SPEC §5.4).
        issue: The normalized issue exposed as the ``issue`` template variable.
        attempt: ``None``/absent on the first run; an integer on retry or
            continuation (SPEC §12.3). Always provided to the template so it can
            branch on it under strict variable checking.

    Returns:
        The rendered prompt.

    Raises:
        TemplateParseError: The template has a syntax error.
        TemplateRenderError: An unknown variable/filter or invalid interpolation
            was encountered while rendering.
    """
    source = template_source if template_source.strip() else DEFAULT_PROMPT
    try:
        template = _ENVIRONMENT.from_string(source)
        return template.render(issue=build_issue_context(issue), attempt=attempt)
    except LiquidSyntaxError as exc:
        raise TemplateParseError(str(exc)) from exc
    except LiquidError as exc:
        raise TemplateRenderError(str(exc)) from exc
