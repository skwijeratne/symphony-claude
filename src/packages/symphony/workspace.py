"""Per-issue workspace creation/reuse and safety invariants (SPEC §9.1-9.2, §9.5).

Each issue runs in its own directory ``<workspace.root>/<workspace_key>`` (SPEC
§9.1). :func:`ensure_workspace` sanitizes the issue identifier, computes that path,
and creates it if missing — reporting ``created_now`` so the caller can gate the
``after_create`` hook (SPEC §9.2). Workspaces are reused across runs and are never
auto-deleted here (SPEC §9.1).

The §9.5 safety invariants are the most important portability constraint, so they
are enforced as standalone, side-effect-free checks that the agent runner MUST call
before launching the subprocess:

* :func:`validate_within_root` — Invariant 2: the workspace path must stay inside
  the workspace root. This is what stops a crafted identifier from escaping the
  root: :func:`~symphony.normalization.sanitize_workspace_key` (Invariant 3) maps
  path separators to ``_``, but ``.`` is an allowed character, so an identifier like
  ``..`` survives sanitization unchanged and would otherwise resolve to the root's
  parent. The containment check rejects it.
* :func:`validate_agent_cwd` — Invariant 1: the subprocess ``cwd`` must equal the
  workspace path.

Scope: this module creates the directory and returns ``created_now``; it does *not*
run lifecycle hooks (``after_create`` and friends are SPEC §9.4, owned by a later
PR) nor populate/sync the workspace contents (SPEC §9.3, implementation-defined).
"""

from __future__ import annotations

import os
from pathlib import Path

from symphony.exceptions import (
    InvalidWorkspaceCwdError,
    InvalidWorkspacePathError,
    WorkspaceCreationError,
)
from symphony.models import Workspace
from symphony.normalization import sanitize_workspace_key

__all__ = [
    "workspace_path_for",
    "ensure_workspace",
    "validate_within_root",
    "validate_agent_cwd",
]


def _normalize_abs(path: Path) -> Path:
    """Normalize ``path`` to an absolute, lexically-collapsed form.

    Uses :func:`os.path.abspath` (not :meth:`Path.resolve`) so ``..`` segments are
    collapsed without following symlinks — matching how ``workspace.root`` itself is
    normalized in the config layer (SPEC §6.1), which keeps containment comparisons
    predictable.
    """
    return Path(os.path.abspath(path))  # noqa: PTH100


def validate_within_root(workspace_path: Path, root: Path) -> Path:
    """Enforce §9.5 Invariant 2: ``workspace_path`` must stay inside ``root``.

    Both paths are normalized to absolute, lexically-collapsed form and the root
    must be a strict parent directory of the workspace path — the path may not be
    the root itself (the layout is ``<root>/<key>``) nor anywhere outside it.

    Args:
        workspace_path: The candidate per-issue workspace path.
        root: The configured workspace root.

    Returns:
        The normalized absolute workspace path.

    Raises:
        InvalidWorkspacePathError: The path escapes, or coincides with, the root.
    """
    root_abs = _normalize_abs(root)
    path_abs = _normalize_abs(workspace_path)
    if path_abs == root_abs or root_abs not in path_abs.parents:
        raise InvalidWorkspacePathError(
            f"workspace path {path_abs} is not contained within root {root_abs}"
        )
    return path_abs


def workspace_path_for(identifier: str, root: Path) -> Path:
    """Compute the validated per-issue workspace path (SPEC §9.1, §9.5).

    Sanitizes the identifier to a workspace key (Invariant 3), joins it under
    ``root``, and enforces root containment (Invariant 2). Pure: no filesystem side
    effects.

    Args:
        identifier: The human-readable issue identifier (for example ``ABC-123``).
        root: The normalized absolute workspace root from configuration.

    Returns:
        The normalized absolute workspace path.

    Raises:
        InvalidWorkspacePathError: The sanitized identifier escapes the root (for
            example an identifier of ``..``) or produces no usable key.
    """
    key = sanitize_workspace_key(identifier)
    return validate_within_root(root / key, root)


def ensure_workspace(identifier: str, root: Path) -> Workspace:
    """Create or reuse the per-issue workspace directory (SPEC §9.2).

    Computes the validated path, then ensures it exists as a directory. The
    directory (and any missing parents, including the root) is created if absent;
    an existing directory is reused. ``created_now`` is ``True`` only when this call
    created the directory, which gates the ``after_create`` hook (run by the
    workspace-hooks PR, SPEC §9.4).

    Args:
        identifier: The human-readable issue identifier.
        root: The normalized absolute workspace root from configuration.

    Returns:
        The :class:`~symphony.models.Workspace` describing the path, sanitized key,
        and whether it was just created.

    Raises:
        InvalidWorkspacePathError: The path escapes the root (SPEC §9.5), or an
            existing non-directory occupies the workspace location (SPEC §17.2).
        WorkspaceCreationError: The directory could not be created (SPEC §9.3,
            §14.1).
    """
    key = sanitize_workspace_key(identifier)
    path = validate_within_root(root / key, root)

    try:
        path.mkdir(parents=True)
        created_now = True
    except FileExistsError:
        # Something already occupies the location; reuse it only if it is a
        # directory, otherwise fail rather than run the agent against a file.
        created_now = False
        if not path.is_dir():
            raise InvalidWorkspacePathError(
                f"workspace location {path} exists but is not a directory"
            ) from None
    except OSError as exc:
        raise WorkspaceCreationError(
            f"could not create workspace directory {path}: {exc}"
        ) from exc

    return Workspace(path=path, workspace_key=key, created_now=created_now)


def validate_agent_cwd(cwd: Path, workspace_path: Path) -> None:
    """Enforce §9.5 Invariant 1: the agent ``cwd`` must equal the workspace path.

    Called by the agent runner immediately before launching the coding-agent
    subprocess (SPEC §9.5, §10). Both paths are normalized to absolute,
    lexically-collapsed form before comparison.

    Args:
        cwd: The working directory the subprocess would be launched in.
        workspace_path: The validated per-issue workspace path.

    Raises:
        InvalidWorkspaceCwdError: ``cwd`` does not match the workspace path.
    """
    cwd_abs = _normalize_abs(cwd)
    workspace_abs = _normalize_abs(workspace_path)
    if cwd_abs != workspace_abs:
        raise InvalidWorkspaceCwdError(
            f"agent cwd {cwd_abs} does not match workspace path {workspace_abs}"
        )
