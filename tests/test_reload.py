"""Tests for dynamic ``WORKFLOW.md`` reload (SPEC §6.2, §17.1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from symphony.exceptions import (
    MissingWorkflowFileError,
    WorkflowConfigError,
    WorkflowParseError,
)
from symphony.reload import EffectiveConfig, ReloadOutcome, WorkflowReloader

# A minimal, fully dispatchable workflow; ``interval_ms`` is the knob the tests
# mutate to prove a reload re-applied new config.
_BASE = """\
---
tracker:
  kind: linear
  api_key: lin_secret
  project_slug: my-team
polling:
  interval_ms: {interval}
---
Work on {{{{ issue.identifier }}}}.
"""


def _write(path: Path, *, interval: int = 30000, body: str | None = None) -> None:
    path.write_text(body if body is not None else _BASE.format(interval=interval))


def _reloader(tmp_path: Path, **kwargs: object) -> WorkflowReloader:
    path = tmp_path / "WORKFLOW.md"
    _write(path)
    return WorkflowReloader(path, env={}, **kwargs)  # type: ignore[arg-type]


# --- initial load -------------------------------------------------------------
def test_initial_load_adopts_config_and_template(tmp_path: Path) -> None:
    reloader = _reloader(tmp_path)
    current = reloader.current
    assert isinstance(current, EffectiveConfig)
    assert current.config.polling.interval_ms == 30000
    assert current.config.tracker.project_slug == "my-team"
    assert current.prompt_template == "Work on {{ issue.identifier }}."


def test_initial_load_failure_propagates(tmp_path: Path) -> None:
    # Missing file must fail startup rather than silently start with no config.
    with pytest.raises(MissingWorkflowFileError):
        WorkflowReloader(tmp_path / "WORKFLOW.md", env={})


def test_relative_workspace_root_resolves_against_workflow_dir(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(
        path,
        body=(
            "---\n"
            "tracker: {kind: linear, api_key: k, project_slug: s}\n"
            "workspace: {root: ./ws}\n"
            "---\n"
            "body\n"
        ),
    )
    reloader = WorkflowReloader(path, env={})
    assert reloader.current.config.workspace.root == tmp_path / "ws"


# --- reload: success ----------------------------------------------------------
def test_reload_reapplies_changed_config(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path, interval=30000)
    reloader = WorkflowReloader(path, env={})

    _write(path, interval=5000)
    outcome = reloader.reload()

    assert isinstance(outcome, ReloadOutcome)
    assert outcome.applied is True
    assert outcome.error is None
    assert outcome.effective.config.polling.interval_ms == 5000
    assert reloader.current.config.polling.interval_ms == 5000


def test_reload_picks_up_new_prompt_template(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path)
    reloader = WorkflowReloader(path, env={})

    _write(
        path,
        body=(
            "---\ntracker: {kind: linear, api_key: k, project_slug: s}\n"
            "---\nNEW BODY\n"
        ),
    )
    reloader.reload()
    assert reloader.current.prompt_template == "NEW BODY"


# --- reload: last-known-good on invalid (SPEC §6.2, §17.1) --------------------
def test_invalid_reload_keeps_last_known_good_and_emits_error(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path, interval=30000)
    errors: list[WorkflowConfigError] = []
    reloader = WorkflowReloader(path, env={}, on_error=errors.append)

    # Unterminated front matter -> WorkflowParseError on reload.
    _write(path, body="---\ntracker: {kind: linear}\nno closing fence\n")
    outcome = reloader.reload()

    assert outcome.applied is False
    assert isinstance(outcome.error, WorkflowParseError)
    # Last known good is retained, both in the outcome and as current.
    assert outcome.effective.config.polling.interval_ms == 30000
    assert reloader.current.config.polling.interval_ms == 30000
    # Operator-visible error was emitted exactly once.
    assert len(errors) == 1 and isinstance(errors[0], WorkflowParseError)


def test_reload_recovers_after_a_failed_reload(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path, interval=30000)
    reloader = WorkflowReloader(path, env={})

    _write(path, body="---\nnot: [closed\n")  # invalid YAML
    assert reloader.reload().applied is False

    _write(path, interval=1234)  # fixed again
    outcome = reloader.reload()
    assert outcome.applied is True
    assert reloader.current.config.polling.interval_ms == 1234


def test_on_error_not_called_on_success(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path)
    errors: list[WorkflowConfigError] = []
    reloader = WorkflowReloader(path, env={}, on_error=errors.append)

    _write(path, interval=7000)
    reloader.reload()
    assert errors == []


def test_resolvable_but_undispatchable_config_is_still_applied(tmp_path: Path) -> None:
    # A config that drops the api_key still parses+resolves, so reload applies it
    # (SPEC §6.2). Dispatch gating for it is preflight's job (SPEC §6.3), not here.
    path = tmp_path / "WORKFLOW.md"
    _write(path)
    reloader = WorkflowReloader(path, env={})

    _write(
        path,
        body=(
            "---\ntracker: {kind: linear, project_slug: s}\n"
            "polling: {interval_ms: 9000}\n---\nb\n"
        ),
    )
    outcome = reloader.reload()
    assert outcome.applied is True
    assert outcome.effective.config.tracker.api_key is None
    assert outcome.effective.config.polling.interval_ms == 9000


# --- poll: change detection (SPEC §6.2 defensive re-validate) -----------------
def test_poll_returns_none_when_unchanged(tmp_path: Path) -> None:
    reloader = _reloader(tmp_path)
    assert reloader.poll() is None
    assert reloader.poll() is None


def test_poll_reloads_when_content_changes(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path, interval=30000)
    reloader = WorkflowReloader(path, env={})

    _write(path, interval=2500)
    outcome = reloader.poll()
    assert outcome is not None and outcome.applied is True
    assert reloader.current.config.polling.interval_ms == 2500
    # Now stable again.
    assert reloader.poll() is None


def test_poll_detects_mtime_only_change(tmp_path: Path) -> None:
    # Same byte length, different mtime: the signature includes mtime so a same-size
    # edit is still detected.
    path = tmp_path / "WORKFLOW.md"
    _write(path, interval=10000)
    reloader = WorkflowReloader(path, env={})

    _write(path, interval=20000)  # same number of digits -> identical size
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    outcome = reloader.poll()
    assert outcome is not None and outcome.applied is True
    assert reloader.current.config.polling.interval_ms == 20000


def test_poll_after_delete_keeps_last_known_good_then_settles(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path, interval=30000)
    errors: list[WorkflowConfigError] = []
    reloader = WorkflowReloader(path, env={}, on_error=errors.append)

    path.unlink()
    outcome = reloader.poll()
    assert outcome is not None and outcome.applied is False
    assert isinstance(outcome.error, MissingWorkflowFileError)
    assert reloader.current.config.polling.interval_ms == 30000
    # A still-missing file is no longer a "change": no repeated reload attempts.
    assert reloader.poll() is None
    assert len(errors) == 1


def test_poll_recovers_when_file_reappears(tmp_path: Path) -> None:
    path = tmp_path / "WORKFLOW.md"
    _write(path, interval=30000)
    reloader = WorkflowReloader(path, env={})

    path.unlink()
    assert reloader.poll().applied is False  # type: ignore[union-attr]

    _write(path, interval=4321)
    outcome = reloader.poll()
    assert outcome is not None and outcome.applied is True
    assert reloader.current.config.polling.interval_ms == 4321
