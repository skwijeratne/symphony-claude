"""Tests for strict prompt rendering (SPEC §5.4, §12, §17.1)."""

from __future__ import annotations

from typing import Any

import pytest

from symphony.exceptions import TemplateParseError, TemplateRenderError
from symphony.models import BlockerRef, Issue
from symphony.prompt_renderer import DEFAULT_PROMPT, render_prompt


def _issue(**overrides: Any) -> Issue:
    base: dict[str, Any] = {
        "id": "iss_1",
        "identifier": "ABC-123",
        "title": "Fix the bug",
        "state": "Todo",
    }
    base.update(overrides)
    return Issue(**base)


def test_renders_issue_fields():
    out = render_prompt("Work on {{ issue.identifier }}: {{ issue.title }}", _issue())
    assert out == "Work on ABC-123: Fix the bug"


def test_renders_labels_iteration():
    issue = _issue(labels=["bug", "p1"])
    out = render_prompt(
        "{% for label in issue.labels %}{{ label }} {% endfor %}", issue
    )
    assert out == "bug p1 "


def test_renders_blockers_iteration():
    issue = _issue(blocked_by=[BlockerRef(identifier="ABC-1", state="In Progress")])
    out = render_prompt(
        "{% for b in issue.blocked_by %}{{ b.identifier }}={{ b.state }}{% endfor %}",
        issue,
    )
    assert out == "ABC-1=In Progress"


def test_renders_derived_fields():
    issue = _issue(identifier="feature/login", state="In Progress")
    out = render_prompt("{{ issue.workspace_key }}|{{ issue.normalized_state }}", issue)
    assert out == "feature_login|in progress"


def test_attempt_absent_on_first_run_renders_empty():
    out = render_prompt("attempt=[{{ attempt }}]", _issue())
    assert out == "attempt=[]"


def test_attempt_available_on_retry():
    out = render_prompt("attempt=[{{ attempt }}]", _issue(), attempt=3)
    assert out == "attempt=[3]"


def test_attempt_branching():
    template = "{% if attempt %}retry {{ attempt }}{% else %}first run{% endif %}"
    assert render_prompt(template, _issue()) == "first run"
    assert render_prompt(template, _issue(), attempt=2) == "retry 2"


def test_empty_template_uses_default_prompt():
    assert render_prompt("", _issue()) == DEFAULT_PROMPT


def test_whitespace_template_uses_default_prompt():
    assert render_prompt("   \n\t  ", _issue()) == DEFAULT_PROMPT


def test_unknown_variable_fails_rendering():
    with pytest.raises(TemplateRenderError) as exc:
        render_prompt("{{ nope }}", _issue())
    assert exc.value.code == "template_render_error"


def test_unknown_issue_field_fails_rendering():
    with pytest.raises(TemplateRenderError):
        render_prompt("{{ issue.does_not_exist }}", _issue())


def test_unknown_filter_fails_rendering():
    with pytest.raises(TemplateRenderError) as exc:
        render_prompt("{{ issue.identifier | no_such_filter }}", _issue())
    assert exc.value.code == "template_render_error"


def test_syntax_error_fails_parsing():
    # Unbalanced tag: a 'for' with no matching 'endfor'.
    with pytest.raises(TemplateParseError) as exc:
        render_prompt("{% for x in issue.labels %}{{ x }}", _issue())
    assert exc.value.code == "template_parse_error"
