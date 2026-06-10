"""Tests for workspace lifecycle hook execution (SPEC §9.4, §17.2)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from symphony.config import HooksConfig
from symphony.exceptions import HookExecutionError, HookTimeoutError
from symphony.hooks import HookKind, run_after_create, run_hook
from symphony.models import Workspace

# The two fatal hooks abort their operation; the two best-effort hooks never do.
_FATAL = [HookKind.AFTER_CREATE, HookKind.BEFORE_RUN]
_BEST_EFFORT = [HookKind.AFTER_RUN, HookKind.BEFORE_REMOVE]


def _config(
    kind: HookKind, script: str | None, *, timeout_ms: int = 60000
) -> HooksConfig:
    """A HooksConfig with just ``kind`` set to ``script``."""
    return HooksConfig(**{kind.value: script}, timeout_ms=timeout_ms)


# --- no-op when unconfigured (§9.4) -------------------------------------------
@pytest.mark.parametrize("kind", list(HookKind))
def test_unset_hook_is_a_noop(kind: HookKind, tmp_path: Path) -> None:
    assert run_hook(kind, _config(kind, None), tmp_path) is True


@pytest.mark.parametrize("kind", list(HookKind))
def test_blank_hook_is_a_noop(kind: HookKind, tmp_path: Path) -> None:
    assert run_hook(kind, _config(kind, "   \n  "), tmp_path) is True


# --- success ------------------------------------------------------------------
@pytest.mark.parametrize("kind", list(HookKind))
def test_successful_hook_returns_true(kind: HookKind, tmp_path: Path) -> None:
    assert run_hook(kind, _config(kind, "exit 0"), tmp_path) is True


def test_hook_runs_in_the_workspace_directory(tmp_path: Path) -> None:
    # The hook's cwd is the workspace, so a relative write lands inside it (§9.4).
    run_hook(
        HookKind.AFTER_CREATE, _config(HookKind.AFTER_CREATE, "touch marker"), tmp_path
    )
    assert (tmp_path / "marker").exists()


# --- fatal hooks raise on failure (§9.4) --------------------------------------
@pytest.mark.parametrize("kind", _FATAL)
def test_fatal_hook_nonzero_exit_raises(kind: HookKind, tmp_path: Path) -> None:
    with pytest.raises(HookExecutionError):
        run_hook(kind, _config(kind, "exit 3"), tmp_path)


@pytest.mark.parametrize("kind", _FATAL)
def test_fatal_hook_timeout_raises(kind: HookKind, tmp_path: Path) -> None:
    # Tiny timeout against a long sleep: TimeoutExpired fires in ~50ms.
    with pytest.raises(HookTimeoutError):
        run_hook(kind, _config(kind, "sleep 5", timeout_ms=50), tmp_path)


@pytest.mark.parametrize("kind", _FATAL)
def test_fatal_hook_spawn_failure_raises(kind: HookKind, tmp_path: Path) -> None:
    # A non-existent cwd makes the spawn itself fail (OSError) before the shell runs.
    missing = tmp_path / "gone"
    with pytest.raises(HookExecutionError):
        run_hook(kind, _config(kind, "exit 0"), missing)


# --- best-effort hooks log and ignore failure (§9.4) --------------------------
@pytest.mark.parametrize("kind", _BEST_EFFORT)
def test_best_effort_hook_nonzero_exit_is_ignored(
    kind: HookKind, tmp_path: Path
) -> None:
    assert run_hook(kind, _config(kind, "exit 3"), tmp_path) is False


@pytest.mark.parametrize("kind", _BEST_EFFORT)
def test_best_effort_hook_timeout_is_ignored(kind: HookKind, tmp_path: Path) -> None:
    assert run_hook(kind, _config(kind, "sleep 5", timeout_ms=50), tmp_path) is False


# --- after_create gating on created_now (§9.2) --------------------------------
def _workspace(tmp_path: Path, *, created_now: bool) -> Workspace:
    return Workspace(path=tmp_path, workspace_key="ABC-123", created_now=created_now)


def test_run_after_create_runs_for_new_workspace(tmp_path: Path) -> None:
    config = _config(HookKind.AFTER_CREATE, "touch created")
    assert run_after_create(config, _workspace(tmp_path, created_now=True)) is True
    assert (tmp_path / "created").exists()


def test_run_after_create_skips_reused_workspace(tmp_path: Path) -> None:
    # A failing hook would raise if it ran; reuse must skip it entirely.
    config = _config(HookKind.AFTER_CREATE, "exit 1")
    assert run_after_create(config, _workspace(tmp_path, created_now=False)) is True


def test_run_after_create_propagates_failure_for_new_workspace(tmp_path: Path) -> None:
    config = _config(HookKind.AFTER_CREATE, "exit 1")
    with pytest.raises(HookExecutionError):
        run_after_create(config, _workspace(tmp_path, created_now=True))


# --- logging (§9.4: log start and failures) -----------------------------------
def test_hook_logs_start_and_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="symphony.hooks"):
        run_hook(HookKind.AFTER_RUN, _config(HookKind.AFTER_RUN, "exit 1"), tmp_path)
    messages = [record.message for record in caplog.records]
    assert any("running after_run hook" in message for message in messages)
    assert any("ignored" in message for message in messages)
