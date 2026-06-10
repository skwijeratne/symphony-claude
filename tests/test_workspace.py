"""Tests for workspace creation/reuse and safety invariants (SPEC §9, §17.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from symphony.exceptions import (
    InvalidWorkspaceCwdError,
    InvalidWorkspacePathError,
    WorkspaceCreationError,
)
from symphony.workspace import (
    ensure_workspace,
    validate_agent_cwd,
    validate_within_root,
    workspace_path_for,
)


# --- path computation + sanitization (§9.1, §9.5 Invariant 3) -----------------
def test_path_is_deterministic_per_identifier(tmp_path: Path) -> None:
    first = workspace_path_for("ABC-123", tmp_path)
    second = workspace_path_for("ABC-123", tmp_path)
    assert first == second == tmp_path / "ABC-123"


def test_disallowed_characters_are_sanitized_in_path(tmp_path: Path) -> None:
    # Slashes/spaces are not in [A-Za-z0-9._-]; each becomes a single underscore.
    path = workspace_path_for("team/feature 1", tmp_path)
    assert path == tmp_path / "team_feature_1"


# --- containment (§9.5 Invariant 2) -------------------------------------------
def test_dotdot_identifier_is_rejected(tmp_path: Path) -> None:
    # '..' survives sanitization ('.' is allowed) and would escape the root.
    with pytest.raises(InvalidWorkspacePathError):
        workspace_path_for("..", tmp_path)


def test_single_dot_identifier_is_rejected(tmp_path: Path) -> None:
    # '.' resolves to the root itself, which is not a per-issue subdirectory.
    with pytest.raises(InvalidWorkspacePathError):
        workspace_path_for(".", tmp_path)


def test_empty_identifier_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidWorkspacePathError):
        workspace_path_for("", tmp_path)


def test_validate_within_root_accepts_child(tmp_path: Path) -> None:
    child = tmp_path / "ABC-123"
    assert validate_within_root(child, tmp_path) == child


def test_validate_within_root_rejects_outside_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere"
    with pytest.raises(InvalidWorkspacePathError):
        validate_within_root(outside, tmp_path)


def test_validate_within_root_rejects_root_itself(tmp_path: Path) -> None:
    with pytest.raises(InvalidWorkspacePathError):
        validate_within_root(tmp_path, tmp_path)


# --- create / reuse (§9.2) ----------------------------------------------------
def test_missing_workspace_is_created(tmp_path: Path) -> None:
    workspace = ensure_workspace("ABC-123", tmp_path)
    assert workspace.path == tmp_path / "ABC-123"
    assert workspace.workspace_key == "ABC-123"
    assert workspace.created_now is True
    assert workspace.path.is_dir()


def test_root_is_created_when_absent(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    workspace = ensure_workspace("ABC-123", root)
    assert workspace.created_now is True
    assert workspace.path.is_dir()


def test_existing_workspace_is_reused(tmp_path: Path) -> None:
    first = ensure_workspace("ABC-123", tmp_path)
    # Drop a marker file to prove the directory is reused, not recreated.
    (first.path / "marker").write_text("kept")

    second = ensure_workspace("ABC-123", tmp_path)
    assert second.path == first.path
    assert second.created_now is False
    assert (second.path / "marker").read_text() == "kept"


def test_created_now_gates_only_first_call(tmp_path: Path) -> None:
    assert ensure_workspace("ABC-123", tmp_path).created_now is True
    assert ensure_workspace("ABC-123", tmp_path).created_now is False


def test_non_directory_at_location_is_rejected(tmp_path: Path) -> None:
    # A file already occupying the workspace path must fail, not be run against.
    clash = tmp_path / "ABC-123"
    clash.write_text("i am a file")
    with pytest.raises(InvalidWorkspacePathError):
        ensure_workspace("ABC-123", tmp_path)


def test_ensure_workspace_rejects_escaping_identifier(tmp_path: Path) -> None:
    with pytest.raises(InvalidWorkspacePathError):
        ensure_workspace("..", tmp_path)


def test_root_is_a_file_raises_creation_error(tmp_path: Path) -> None:
    root = tmp_path / "root-file"
    root.write_text("not a dir")
    with pytest.raises(WorkspaceCreationError):
        ensure_workspace("ABC-123", root)


# --- agent cwd invariant (§9.5 Invariant 1) -----------------------------------
def test_validate_agent_cwd_accepts_matching_path(tmp_path: Path) -> None:
    workspace = ensure_workspace("ABC-123", tmp_path)
    # Equivalent but non-normalized spelling still matches after normalization.
    spelled = tmp_path / "." / "ABC-123"
    validate_agent_cwd(spelled, workspace.path)


def test_validate_agent_cwd_rejects_mismatch(tmp_path: Path) -> None:
    workspace = ensure_workspace("ABC-123", tmp_path)
    with pytest.raises(InvalidWorkspaceCwdError):
        validate_agent_cwd(tmp_path, workspace.path)
