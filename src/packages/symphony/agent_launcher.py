"""Claude Code headless launch: flag building + subprocess launch (SPEC §10.1).

Each turn is one ``claude --print`` subprocess launched in the per-issue workspace,
streaming newline-delimited JSON on stdout (SPEC §10). This module owns just the
*launch* half of the agent runner: turning a :class:`ClaudeConfig` (plus session
state) into the runtime-managed and optional flags (§10.1), assembling the
``bash -lc "<command> <flags>"`` invocation, and starting the subprocess with the
workspace as ``cwd`` and the rendered prompt delivered on stdin.

Parsing the ``stream-json`` stream, turn lifecycle/outcome, and timeouts are later
PRs (SPEC §10.2-10.4, §10.6); :func:`launch_turn` returns the live
:class:`subprocess.Popen` for them to drive.

Flag values are shell-quoted because the CLI is invoked through ``bash -lc`` with a
single command string: a tool pattern such as ``Bash(npm run test:*)`` contains
spaces, parentheses, and globs that the shell would otherwise interpret. The base
``claude.command`` is inserted verbatim (it may itself be a command line such as
``npx claude``; SPEC §5.3.6).

Flag names follow the targeted Claude Code CLI surface (SPEC §10.1); per SPEC §10
the CLI is the source of truth, so these are expected to be verified against
``claude --help`` for the pinned version.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from symphony.config import ClaudeConfig
from symphony.exceptions import ClaudeNotFoundError, InvalidWorkspaceCwdError

__all__ = ["build_flags", "build_command_line", "launch_turn"]

_OUTPUT_FORMAT = "stream-json"

# Tool lists are passed as a single comma-joined argument. Commas do not appear in
# tool patterns (whereas spaces can, e.g. ``Bash(npm run test:*)``), so comma-joining
# is lossless; the whole value is shell-quoted by build_command_line. The exact
# accepted form is CLI-version-dependent (SPEC §10) — isolated here for easy change.
_TOOL_SEPARATOR = ","


def build_flags(
    config: ClaudeConfig,
    *,
    session_id: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build the ``claude`` argument tokens for one turn (SPEC §10.1).

    Args:
        config: The resolved ``claude`` config block.
        session_id: A runtime-generated session id to fix via ``--session-id`` on a
            first turn. Omit to let the CLI assign one (captured later from the
            ``system``/``init`` event).
        resume_session_id: The session id to continue via ``--resume`` on a
            continuation turn.

    Returns:
        The ordered flag tokens (unquoted), starting with the runtime-managed flags
        and followed by the optional flags present in ``config``.

    Raises:
        ValueError: Both ``session_id`` and ``resume_session_id`` were given; a turn
            is either a first turn or a continuation, not both.
    """
    if session_id is not None and resume_session_id is not None:
        raise ValueError("pass at most one of session_id / resume_session_id")

    flags = ["--print", "--output-format", _OUTPUT_FORMAT, "--verbose"]

    if resume_session_id is not None:
        flags += ["--resume", resume_session_id]
    elif session_id is not None:
        flags += ["--session-id", session_id]

    if config.model:
        flags += ["--model", config.model]
    if config.permission_mode:
        flags += ["--permission-mode", config.permission_mode]
    if config.allowed_tools:
        flags += ["--allowedTools", _TOOL_SEPARATOR.join(config.allowed_tools)]
    if config.disallowed_tools:
        flags += ["--disallowedTools", _TOOL_SEPARATOR.join(config.disallowed_tools)]
    if config.append_system_prompt:
        flags += ["--append-system-prompt", config.append_system_prompt]
    if config.mcp_config:
        flags += ["--mcp-config", config.mcp_config]
    for directory in config.add_dirs:
        flags += ["--add-dir", directory]

    flags += list(config.extra_args)
    return flags


def build_command_line(config: ClaudeConfig, flags: Sequence[str]) -> str:
    """Assemble the shell command string for ``bash -lc`` (SPEC §10.1).

    The base command is inserted verbatim; every flag token is shell-quoted so
    special characters in tool patterns/values are not reinterpreted by the shell.
    """
    quoted_flags = " ".join(shlex.quote(flag) for flag in flags)
    return f"{config.command} {quoted_flags}"


def launch_turn(
    config: ClaudeConfig,
    *,
    workspace_path: Path,
    prompt: str,
    session_id: str | None = None,
    resume_session_id: str | None = None,
) -> subprocess.Popen[str]:
    """Launch one headless ``claude`` turn subprocess (SPEC §10.1).

    Runs ``bash -lc "<claude.command> <flags>"`` with the workspace as ``cwd``,
    stdout/stderr captured as separate pipes, and ``prompt`` written to stdin (the
    preferred prompt channel for large prompts; SPEC §10.1).

    Args:
        config: The resolved ``claude`` config block.
        workspace_path: The per-issue workspace; becomes the subprocess ``cwd``
            (SPEC §9.5, §10.1).
        prompt: The rendered prompt for this turn.
        session_id: Fix the session id on a first turn (``--session-id``).
        resume_session_id: Continue a session on a continuation turn (``--resume``).

    Returns:
        The running subprocess, with text-mode stdin already closed and stdout/stderr
        pipes open for the streaming reader.

    Raises:
        InvalidWorkspaceCwdError: ``workspace_path`` is not an existing directory.
        ClaudeNotFoundError: The launcher subprocess could not be started.
        ValueError: Both session arguments were given (see :func:`build_flags`).
    """
    if not workspace_path.is_dir():
        raise InvalidWorkspaceCwdError(
            f"workspace cwd is not a directory: {workspace_path}"
        )

    flags = build_flags(
        config, session_id=session_id, resume_session_id=resume_session_id
    )
    command_line = build_command_line(config, flags)

    try:
        process = subprocess.Popen(
            ["bash", "-lc", command_line],
            cwd=workspace_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered: the streaming reader consumes events by line
        )
    except OSError as exc:
        raise ClaudeNotFoundError(f"could not launch agent subprocess: {exc}") from exc

    _write_prompt(process, prompt)
    return process


def _write_prompt(process: subprocess.Popen[str], prompt: str) -> None:
    """Deliver the prompt on stdin and signal EOF (SPEC §10.1).

    A ``BrokenPipeError`` means the subprocess exited before reading the prompt; that
    is surfaced later through the exit code / missing ``result`` rather than as a
    launch failure.
    """
    if process.stdin is None:  # pragma: no cover - stdin=PIPE always sets this
        return
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:  # pragma: no cover - timing-dependent early exit
        pass
