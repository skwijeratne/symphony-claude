"""Tests for the CLI entrypoint and host lifecycle (SPEC §17.7, §16.1)."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

from symphony.cli import (
    EXIT_STARTUP_FAILURE,
    EXIT_SUCCESS,
    _parse_var_assignment,
    main,
    run_application,
)
from symphony.exceptions import DispatchPreflightError, MissingWorkflowFileError
from symphony.reload import WorkflowConfigStore
from symphony.structured_logging import SYMPHONY_LOGGER_NAME

# A minimal workflow that passes dispatch preflight (SPEC §6.3).
_DISPATCHABLE = """\
---
tracker:
  kind: linear
  api_key: lin_secret
  project_slug: my-team
---
Work on {{ issue.identifier }}.
"""


@pytest.fixture(autouse=True)
def _restore_symphony_logger() -> Iterator[None]:
    """Undo ``configure_logging``'s global handler/level changes after each test."""
    root = logging.getLogger(SYMPHONY_LOGGER_NAME)
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _workflow(tmp_path: Path, body: str = _DISPATCHABLE) -> Path:
    path = tmp_path / "WORKFLOW.md"
    path.write_text(body)
    return path


# --- workflow path argument (SPEC §17.7) ------------------------------------------
def test_explicit_workflow_path_is_passed_to_the_application(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    seen: list[Path] = []

    def run_app(workflow_path: Path) -> int:
        seen.append(workflow_path)
        return EXIT_SUCCESS

    assert main([str(path)], run_app=run_app) == EXIT_SUCCESS
    assert seen == [path]


def test_default_workflow_path_is_cwd_workflow_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _workflow(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: list[Path] = []

    def run_app(workflow_path: Path) -> int:
        seen.append(workflow_path)
        return EXIT_SUCCESS

    assert main([], run_app=run_app) == EXIT_SUCCESS
    assert [p.resolve() for p in seen] == [path.resolve()]


def test_nonexistent_explicit_path_fails_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "nope" / "WORKFLOW.md"

    code = main([str(missing)], run_app=lambda _: EXIT_SUCCESS)

    assert code == EXIT_STARTUP_FAILURE
    assert "startup failed" in caplog.text
    assert "error_code=missing_workflow_file" in caplog.text


def test_missing_default_workflow_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)  # no WORKFLOW.md here

    code = main([], run_app=lambda _: EXIT_SUCCESS)

    assert code == EXIT_STARTUP_FAILURE
    assert "error_code=missing_workflow_file" in caplog.text


def test_usage_error_exits_2_via_argparse() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--bogus"])
    assert excinfo.value.code == 2


# --- logging level (SPEC §16.1) ---------------------------------------------------
def test_default_logging_level_is_info(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    main([str(path)], run_app=lambda _: EXIT_SUCCESS)
    assert logging.getLogger(SYMPHONY_LOGGER_NAME).level == logging.INFO


def test_debug_flag_lowers_logging_level_to_debug(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    main([str(path), "--debug"], run_app=lambda _: EXIT_SUCCESS)
    assert logging.getLogger(SYMPHONY_LOGGER_NAME).level == logging.DEBUG


def test_debug_flag_short_form(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    main([str(path), "-d"], run_app=lambda _: EXIT_SUCCESS)
    assert logging.getLogger(SYMPHONY_LOGGER_NAME).level == logging.DEBUG


# --- --var environment injection (SPEC §6.1) --------------------------------------
def test_parse_var_assignment_basic() -> None:
    assert _parse_var_assignment("GIT_TOKEN=ghp_x") == ("GIT_TOKEN", "ghp_x")


def test_parse_var_assignment_value_with_equals() -> None:
    # Split on the first '=' only, so token values keeping '=' survive intact.
    assert _parse_var_assignment("B64=aGk=eA==") == ("B64", "aGk=eA==")


def test_parse_var_assignment_empty_value_allowed() -> None:
    assert _parse_var_assignment("EMPTY=") == ("EMPTY", "")


def test_parse_var_assignment_missing_equals_rejected() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_var_assignment("NOEQUALS")


@pytest.mark.parametrize("bad", ["9LEADS=x", "HAS SPACE=x", "HAS-DASH=x", "=x"])
def test_parse_var_assignment_invalid_name_rejected(bad: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_var_assignment(bad)


def test_var_is_injected_into_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INJECTED_TOKEN", raising=False)
    path = _workflow(tmp_path)
    main([str(path), "--var", "INJECTED_TOKEN=secret"], run_app=lambda _: EXIT_SUCCESS)
    assert os.environ["INJECTED_TOKEN"] == "secret"


def test_multiple_vars_all_injected_last_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)
    path = _workflow(tmp_path)
    main(
        [str(path), "--var", "A=1", "--var", "B=2", "--var", "A=3"],
        run_app=lambda _: EXIT_SUCCESS,
    )
    assert os.environ["A"] == "3"
    assert os.environ["B"] == "2"


def test_var_overrides_existing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_TOKEN", "old")
    path = _workflow(tmp_path)
    main([str(path), "--var", "GIT_TOKEN=new"], run_app=lambda _: EXIT_SUCCESS)
    assert os.environ["GIT_TOKEN"] == "new"


def test_malformed_var_exits_2_via_argparse(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main([str(path), "--var", "NOEQUALS"])
    assert excinfo.value.code == 2


def test_var_values_are_not_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SECRET", raising=False)
    path = _workflow(tmp_path)
    with caplog.at_level(logging.INFO, logger=SYMPHONY_LOGGER_NAME):
        main([str(path), "--var", "SECRET=topsecret"], run_app=lambda _: EXIT_SUCCESS)
    assert "topsecret" not in caplog.text
    assert "SECRET" in caplog.text


# --- startup failure and exit codes (SPEC §17.7) -----------------------------------
def test_startup_failure_is_surfaced_cleanly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _workflow(tmp_path)

    def run_app(workflow_path: Path) -> int:
        raise DispatchPreflightError("tracker.api_key is missing")

    code = main([str(path)], run_app=run_app)

    assert code == EXIT_STARTUP_FAILURE
    assert "startup failed" in caplog.text
    assert "error_code=dispatch_preflight_failed" in caplog.text
    assert 'reason="tracker.api_key is missing"' in caplog.text


def test_abnormal_application_exit_returns_nonzero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _workflow(tmp_path)

    def run_app(workflow_path: Path) -> int:
        raise RuntimeError("boom")

    code = main([str(path)], run_app=run_app)

    assert code == EXIT_STARTUP_FAILURE
    assert "abnormal host exit" in caplog.text


def test_application_exit_code_is_passed_through(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    assert main([str(path)], run_app=lambda _: 7) == 7


# --- default application (SPEC §16.1) ----------------------------------------------
class _FakeService:
    """A ``ServiceHost`` double recording lifecycle calls."""

    def __init__(
        self, config_source: WorkflowConfigStore, *, exit_code: int = EXIT_SUCCESS
    ):
        self.config_source = config_source
        self.exit_code = exit_code
        self.served = 0
        self.stopped = 0

    def serve(self) -> int:
        self.served += 1
        return self.exit_code

    def stop(self) -> None:
        self.stopped += 1


def test_run_application_composes_and_serves_the_service(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    services: list[_FakeService] = []

    def factory(config_source: WorkflowConfigStore) -> _FakeService:
        services.append(_FakeService(config_source))
        return services[-1]

    assert run_application(path, service_factory=factory) == EXIT_SUCCESS

    (service,) = services
    assert service.served == 1
    # The config source handed over carries the loaded workflow config.
    assert service.config_source.current.config.tracker.project_slug == "my-team"


def test_run_application_restores_signal_handlers(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

    run_application(path, service_factory=_FakeService)

    after = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    assert after == before


def test_run_application_validates_dispatch_preflight(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        body="---\ntracker:\n  kind: linear\n---\nprompt\n",  # no api_key/slug
    )
    # The default factory composes the real service, whose startup validation
    # rejects an undispatchable config before any work begins (SPEC §6.3, §16.1).
    with pytest.raises(DispatchPreflightError):
        run_application(path)


def test_run_application_propagates_workflow_load_failures(tmp_path: Path) -> None:
    with pytest.raises(MissingWorkflowFileError):
        run_application(tmp_path / "WORKFLOW.md")


def test_main_fails_startup_on_invalid_workflow_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _workflow(tmp_path, body="---\ntracker:\n  kind: linear\n---\nprompt\n")

    code = main([str(path)])

    assert code == EXIT_STARTUP_FAILURE
    assert "error_code=dispatch_preflight_failed" in caplog.text
