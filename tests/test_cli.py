"""Tests for the CLI entrypoint and host lifecycle (SPEC §17.7, §16.1)."""

from __future__ import annotations

import logging
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

from symphony.cli import (
    EXIT_STARTUP_FAILURE,
    EXIT_SUCCESS,
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
