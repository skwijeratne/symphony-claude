"""Tests for Claude headless flag building + launch (SPEC §10.1)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from symphony.agent_launcher import build_command_line, build_flags, launch_turn
from symphony.config import ClaudeConfig
from symphony.exceptions import ClaudeNotFoundError, InvalidWorkspaceCwdError

_REQUIRED = ["--print", "--output-format", "stream-json", "--verbose"]


# --- build_flags (§10.1) ------------------------------------------------------
def test_required_flags_always_present() -> None:
    assert build_flags(ClaudeConfig()) == _REQUIRED


def test_first_turn_session_id_flag() -> None:
    flags = build_flags(ClaudeConfig(), session_id="sess-1")
    assert flags == [*_REQUIRED, "--session-id", "sess-1"]


def test_continuation_resume_flag() -> None:
    flags = build_flags(ClaudeConfig(), resume_session_id="sess-1")
    assert flags == [*_REQUIRED, "--resume", "sess-1"]


def test_session_and_resume_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="at most one"):
        build_flags(ClaudeConfig(), session_id="a", resume_session_id="b")


def test_optional_flags_from_config() -> None:
    config = ClaudeConfig(
        model="opus",
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Bash(npm run test:*)"],
        disallowed_tools=["WebFetch"],
        append_system_prompt="be careful",
        mcp_config="/etc/mcp.json",
        add_dirs=["/repo/a", "/repo/b"],
        extra_args=["--foo", "bar"],
    )
    flags = build_flags(config)
    assert flags[:4] == _REQUIRED
    assert "--model" in flags and flags[flags.index("--model") + 1] == "opus"
    assert flags[flags.index("--permission-mode") + 1] == "bypassPermissions"
    # Tool lists are comma-joined into a single argument.
    assert flags[flags.index("--allowedTools") + 1] == "Read,Bash(npm run test:*)"
    assert flags[flags.index("--disallowedTools") + 1] == "WebFetch"
    assert flags[flags.index("--append-system-prompt") + 1] == "be careful"
    assert flags[flags.index("--mcp-config") + 1] == "/etc/mcp.json"
    # --add-dir is repeated, one per directory.
    assert flags.count("--add-dir") == 2
    # extra_args are appended verbatim, last.
    assert flags[-2:] == ["--foo", "bar"]


def test_unset_optional_flags_are_omitted() -> None:
    flags = build_flags(ClaudeConfig())
    for absent in ("--model", "--permission-mode", "--allowedTools", "--add-dir"):
        assert absent not in flags


# --- build_command_line (§10.1) -----------------------------------------------
def test_command_is_verbatim_and_flags_are_quoted() -> None:
    config = ClaudeConfig(command="npx claude", allowed_tools=["Bash(npm run test:*)"])
    line = build_command_line(config, build_flags(config))
    # Base command is not quoted (it may be a command line itself).
    assert line.startswith("npx claude --print ")
    # The tool pattern with spaces/parens/globs is shell-quoted as one token.
    assert shlex.split(line) == ["npx", "claude", *build_flags(config)]


# --- launch_turn (§10.1) — fake claude binary ---------------------------------
def _fake_claude(tmp_path: Path) -> Path:
    """A stand-in 'claude' that reports its args, cwd, and stdin to stdout."""
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'echo "ARGS:$*"\n'
        'echo "CWD:$(pwd -P)"\n'
        "printf 'STDIN:'\n"
        "cat\n"
    )
    script.chmod(0o755)
    return script


def test_launch_passes_flags_cwd_and_prompt(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = ClaudeConfig(command=str(_fake_claude(tmp_path)))

    process = launch_turn(
        config, workspace_path=workspace, prompt="do the work", session_id="sess-9"
    )
    stdout, _ = process.communicate(timeout=10)

    assert process.returncode == 0
    assert (
        "ARGS:--print --output-format stream-json --verbose --session-id sess-9"
        in stdout
    )
    assert f"CWD:{os.path.realpath(workspace)}" in stdout
    assert "STDIN:do the work" in stdout


def test_launch_rejects_non_directory_cwd(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(InvalidWorkspaceCwdError):
        launch_turn(ClaudeConfig(), workspace_path=missing, prompt="x")


def test_launch_wraps_spawn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("no bash")

    monkeypatch.setattr("symphony.agent_launcher.subprocess.Popen", boom)
    with pytest.raises(ClaudeNotFoundError):
        launch_turn(ClaudeConfig(), workspace_path=tmp_path, prompt="x")
