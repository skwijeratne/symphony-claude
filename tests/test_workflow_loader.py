"""Tests for the WORKFLOW.md loader (SPEC §5.1-5.2, §17.1 loader)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symphony.exceptions import (
    MissingWorkflowFileError,
    WorkflowFrontMatterNotAMapError,
    WorkflowParseError,
)
from symphony.workflow_loader import load_workflow, resolve_workflow_path

WORKFLOW = """\
---
tracker:
  kind: linear
  project_slug: my-team
polling:
  interval_ms: 5000
---
You are working on {{ issue.identifier }}.
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- path precedence (SPEC §5.1, §17.1) ---------------------------------------


def test_resolve_path_prefers_explicit(tmp_path):
    explicit = tmp_path / "custom.md"
    assert resolve_workflow_path(explicit, cwd=tmp_path) == explicit


def test_resolve_path_defaults_to_cwd_workflow_md(tmp_path):
    assert resolve_workflow_path(cwd=tmp_path) == tmp_path / "WORKFLOW.md"


def test_load_uses_explicit_path_over_cwd_default(tmp_path):
    _write(tmp_path / "WORKFLOW.md", "default body")
    explicit = _write(tmp_path / "other.md", "explicit body")
    wf = load_workflow(explicit, cwd=tmp_path)
    assert wf.prompt_template == "explicit body"


def test_load_uses_cwd_default_when_no_explicit_path(tmp_path):
    _write(tmp_path / "WORKFLOW.md", WORKFLOW)
    wf = load_workflow(cwd=tmp_path)
    assert wf.config["tracker"]["kind"] == "linear"


# --- happy path ---------------------------------------------------------------


def test_front_matter_and_body_split(tmp_path):
    wf = load_workflow(_write(tmp_path / "w.md", WORKFLOW))
    # config is the front-matter root, not nested under a "config" key (§5.2).
    assert wf.config["tracker"]["project_slug"] == "my-team"
    assert wf.config["polling"]["interval_ms"] == 5000
    assert "config" not in wf.config
    # body is trimmed (§5.2).
    assert wf.prompt_template == "You are working on {{ issue.identifier }}."


def test_no_front_matter_treats_whole_file_as_body(tmp_path):
    wf = load_workflow(_write(tmp_path / "w.md", "Just a prompt.\nSecond line.\n"))
    assert wf.config == {}
    assert wf.prompt_template == "Just a prompt.\nSecond line."


def test_empty_front_matter_is_empty_config(tmp_path):
    wf = load_workflow(_write(tmp_path / "w.md", "---\n---\nbody\n"))
    assert wf.config == {}
    assert wf.prompt_template == "body"


def test_empty_file_is_empty_definition(tmp_path):
    wf = load_workflow(_write(tmp_path / "w.md", ""))
    assert wf.config == {}
    assert wf.prompt_template == ""


def test_body_may_contain_triple_dash_lines(tmp_path):
    text = "---\nkey: value\n---\nintro\n\n---\n\nmore\n"
    wf = load_workflow(_write(tmp_path / "w.md", text))
    assert wf.config == {"key": "value"}
    # Only the first closing fence terminates front matter; later --- stay in body.
    assert wf.prompt_template == "intro\n\n---\n\nmore"


# --- error surface (SPEC §5.5, §17.1) -----------------------------------------


def test_missing_file_raises_typed_error(tmp_path):
    with pytest.raises(MissingWorkflowFileError) as exc:
        load_workflow(tmp_path / "does_not_exist.md")
    assert exc.value.code == "missing_workflow_file"


def test_invalid_yaml_raises_parse_error(tmp_path):
    bad = _write(tmp_path / "w.md", "---\nkey: [unclosed, list\n---\nbody\n")
    with pytest.raises(WorkflowParseError) as exc:
        load_workflow(bad)
    assert exc.value.code == "workflow_parse_error"


def test_unterminated_front_matter_raises_parse_error(tmp_path):
    bad = _write(tmp_path / "w.md", "---\nkey: value\nstill front matter\n")
    with pytest.raises(WorkflowParseError) as exc:
        load_workflow(bad)
    assert exc.value.code == "workflow_parse_error"


@pytest.mark.parametrize(
    "front_matter",
    [
        "- a\n- b\n",  # sequence
        "just a string\n",  # scalar
        "42\n",  # scalar int
    ],
)
def test_non_map_front_matter_raises_typed_error(tmp_path, front_matter):
    bad = _write(tmp_path / "w.md", f"---\n{front_matter}---\nbody\n")
    with pytest.raises(WorkflowFrontMatterNotAMapError) as exc:
        load_workflow(bad)
    assert exc.value.code == "workflow_front_matter_not_a_map"
