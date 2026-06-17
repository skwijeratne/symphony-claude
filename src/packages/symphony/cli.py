"""CLI entrypoint and host process lifecycle (SPEC §17.7, §16.1).

``symphony [-d/--debug] [--var NAME=VALUE ...] [path-to-WORKFLOW.md]`` — the
positional workflow path is optional and defaults to ``WORKFLOW.md`` in the
current working directory (SPEC §5.1). ``--debug`` lowers the ``symphony`` logger
threshold from INFO to DEBUG. ``--var`` (repeatable) injects an environment
variable for the run, visible to config ``$VAR`` indirection, hook scripts, and
the agent subprocess (SPEC §6.1).

:func:`main` owns the host lifecycle conformance surface (SPEC §17.7): argument
parsing, workflow path resolution, logging configuration (SPEC §16.1
``configure_logging()``), surfacing startup failures cleanly on stderr, and the
exit-code contract (``0`` on a normal start-and-shutdown, nonzero on startup
failure or an abnormal host exit). The application itself is the injected
``run_app`` seam; the default :func:`run_application` composes the real
:class:`~symphony.service.SymphonyService` from the workflow, installs
``SIGINT``/``SIGTERM`` handlers for graceful shutdown, and blocks in its event
loop until stopped (SPEC §16.1).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from symphony.exceptions import SymphonyError, WorkflowConfigError
from symphony.reload import WorkflowConfigStore
from symphony.service import SymphonyService
from symphony.structured_logging import configure_logging, log_fields
from symphony.workflow_loader import resolve_workflow_path

__all__ = [
    "EXIT_SUCCESS",
    "EXIT_STARTUP_FAILURE",
    "RunApp",
    "ServiceHost",
    "ServiceFactory",
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


class ServiceHost(Protocol):
    """What the CLI needs from the composed service (SPEC §16.1)."""

    def serve(self) -> int: ...

    def stop(self) -> None: ...


# Builds the service from the workflow config source; injectable for
# host-lifecycle tests.
ServiceFactory = Callable[[WorkflowConfigStore], ServiceHost]


def _log_reload_rejected(error: WorkflowConfigError) -> None:
    """Operator-visible message for a rejected reload (SPEC §6.2)."""
    logger.error(
        "workflow reload rejected; keeping last known good config %s",
        log_fields(error_code=error.code, reason=error.message),
    )


def run_application(
    workflow_path: Path,
    *,
    service_factory: ServiceFactory = SymphonyService,
) -> int:
    """Default application: compose and run the service (SPEC §16.1).

    Loads and resolves the workflow (initial load failures are fatal startup
    errors, SPEC §16.1), composes the service, installs ``SIGINT``/``SIGTERM``
    handlers that request a graceful shutdown, and blocks in the event loop
    until stopped.

    Args:
        workflow_path: The resolved ``WORKFLOW.md`` path.
        service_factory: Builds the service from the config source; injectable
            so host-lifecycle tests run without the real runtime.

    Returns:
        :data:`EXIT_SUCCESS` after a normal start and graceful shutdown.

    Raises:
        WorkflowConfigError: The workflow failed to load/parse/resolve, or
            startup dispatch validation failed (SPEC §6.3).
    """
    config_source = WorkflowConfigStore(workflow_path, on_error=_log_reload_rejected)
    service = service_factory(config_source)
    logger.info("workflow loaded %s", log_fields(workflow_path=workflow_path))

    saved_handlers: dict[signal.Signals, object] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            saved_handlers[signum] = signal.signal(signum, lambda *_: service.stop())
    try:
        return service.serve()
    finally:
        for signum, handler in saved_handlers.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]


# POSIX environment-variable name (what a shell and ``$VAR`` indirection accept).
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_var_assignment(raw: str) -> tuple[str, str]:
    """Parse a ``--var NAME=VALUE`` token into a ``(name, value)`` pair.

    Splits on the first ``=`` only, so values may themselves contain ``=`` (for
    example base64 tokens). The name must be a valid environment-variable name;
    the value may be empty. Invalid input raises :class:`argparse.ArgumentTypeError`
    so it surfaces as a usage error (exit ``2``).
    """
    name, sep, value = raw.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"expected NAME=VALUE, got {raw!r} (missing '=')"
        )
    if not _VAR_NAME.match(name):
        raise argparse.ArgumentTypeError(
            f"invalid variable name {name!r}: must match [A-Za-z_][A-Za-z0-9_]*"
        )
    return name, value


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
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="enable verbose DEBUG-level logging (default: INFO)",
    )
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        type=_parse_var_assignment,
        help=(
            "inject an environment variable for this run (repeatable); available "
            "to config $VAR indirection, hook scripts, and the agent subprocess. "
            "Overrides an existing env var of the same name. Last wins on "
            "duplicate names."
        ),
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
    configure_logging(level=logging.DEBUG if args.debug else logging.INFO)

    # Inject any --var NAME=VALUE into the process environment before config
    # resolution, so they are visible to $VAR indirection, hook scripts, and the
    # agent subprocess (SPEC §6.1). Last wins; --var overrides an inherited env
    # var. Values are secrets, so only the names are logged.
    cli_vars = dict(args.var)
    os.environ.update(cli_vars)
    if cli_vars:
        logger.info("injected cli vars %s", log_fields(names=",".join(cli_vars)))

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
