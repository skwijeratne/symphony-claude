"""CLI entrypoint and host process lifecycle (SPEC §17.7, §16.1).

``symphony [path-to-WORKFLOW.md]`` — the positional workflow path is optional
and defaults to ``WORKFLOW.md`` in the current working directory (SPEC §5.1).

:func:`main` owns the host lifecycle conformance surface (SPEC §17.7): argument
parsing, workflow path resolution, logging configuration (SPEC §16.1
``configure_logging()``), surfacing startup failures cleanly on stderr, and the
exit-code contract (``0`` on a normal start-and-shutdown, nonzero on startup
failure or an abnormal host exit). The application itself is the injected
``run_app`` seam; the default :func:`run_application` performs the SPEC §16.1
startup sequence as far as M6 builds it — initial workflow load/resolve and
dispatch preflight validation — and then shuts down cleanly. Composing the
event loop behind this seam is M7 (ROADMAP item 26).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from symphony.exceptions import SymphonyError
from symphony.preflight import ensure_dispatchable
from symphony.reload import WorkflowReloader
from symphony.structured_logging import configure_logging, log_fields
from symphony.workflow_loader import resolve_workflow_path

__all__ = [
    "EXIT_SUCCESS",
    "EXIT_STARTUP_FAILURE",
    "RunApp",
    "run_application",
    "main",
]

EXIT_SUCCESS = 0
"""The application started and shut down normally (SPEC §17.7)."""

EXIT_STARTUP_FAILURE = 1
"""Startup failed or the host exited abnormally (SPEC §17.7). Usage errors
exit ``2`` via ``argparse``."""

# Runs the application for a resolved workflow path and returns the process
# exit code. Injectable so host-lifecycle behavior is testable without the
# real service (SPEC §17.7).
RunApp = Callable[[Path], int]

logger = logging.getLogger("symphony.cli")


def run_application(workflow_path: Path) -> int:
    """Default application: the SPEC §16.1 startup sequence as built so far.

    Loads and resolves the workflow (initial load failures are fatal startup
    errors, SPEC §16.1) and runs dispatch preflight validation (SPEC §6.3,
    §16.1 ``fail_startup``). The service event loop is composed in M7; until
    then a validated startup shuts down cleanly.

    Args:
        workflow_path: The resolved ``WORKFLOW.md`` path.

    Returns:
        :data:`EXIT_SUCCESS` on a normal start and shutdown.

    Raises:
        WorkflowConfigError: The workflow failed to load/parse/resolve, or
            dispatch preflight validation failed.
    """
    reloader = WorkflowReloader(workflow_path)
    ensure_dispatchable(reloader.current.config)
    logger.info("startup validated %s", log_fields(workflow_path=workflow_path))
    logger.info(
        "service event loop is not wired yet (M7); shutting down %s",
        log_fields(outcome="completed"),
    )
    return EXIT_SUCCESS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony",
        description=(
            "Orchestrate Claude Code (headless) to deliver tracker-driven work."
        ),
    )
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default=None,
        metavar="path-to-WORKFLOW.md",
        help="workflow file to run (default: ./WORKFLOW.md)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run_app: RunApp | None = None,
) -> int:
    """CLI entrypoint (SPEC §17.7).

    Args:
        argv: Command-line arguments excluding the program name; defaults to
            ``sys.argv[1:]``.
        run_app: Application seam; defaults to :func:`run_application`. A
            testing/composition seam — M7 substitutes the full service here.

    Returns:
        The process exit code: ``0`` for a normal start and shutdown, nonzero
        for a missing workflow file, a startup failure, or an abnormal exit.
    """
    args = _build_parser().parse_args(argv)
    configure_logging()

    workflow_path = resolve_workflow_path(args.workflow_path)
    if not workflow_path.is_file():
        logger.error(
            "startup failed %s",
            log_fields(
                error_code="missing_workflow_file",
                workflow_path=workflow_path,
                reason="workflow file not found",
            ),
        )
        return EXIT_STARTUP_FAILURE

    run = run_app if run_app is not None else run_application
    try:
        return run(workflow_path)
    except SymphonyError as error:
        logger.error(
            "startup failed %s",
            log_fields(
                error_code=error.code,
                workflow_path=workflow_path,
                reason=error.message,
            ),
        )
        return EXIT_STARTUP_FAILURE
    except Exception:
        # An unexpected error is an abnormal host exit (SPEC §17.7): log the
        # traceback for the operator and exit nonzero instead of crashing.
        logger.exception(
            "abnormal host exit %s", log_fields(workflow_path=workflow_path)
        )
        return EXIT_STARTUP_FAILURE
