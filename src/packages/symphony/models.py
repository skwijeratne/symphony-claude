"""Core domain models (SPEC §4.1).

This module holds the normalized issue record used by orchestration, prompt
rendering, and observability. Remaining domain entities (workflow definition,
config view, workspace, run attempt, sessions, retry/orchestrator state) are
added by a later PR; this one delivers :class:`Issue` and :class:`BlockerRef`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from symphony.normalization import normalize_state, sanitize_workspace_key

__all__ = ["BlockerRef", "Issue"]


@dataclass(slots=True)
class BlockerRef:
    """Reference to an issue that blocks another issue (SPEC §4.1.1).

    Each field is best-effort: the tracker may expose only a subset for a given
    blocker, so all three are optional.

    Attributes:
        id: Stable tracker-internal ID of the blocking issue, if known.
        identifier: Human-readable key of the blocking issue, if known.
        state: Current tracker state name of the blocking issue, if known.
    """

    id: str | None = None
    identifier: str | None = None
    state: str | None = None


@dataclass(slots=True)
class Issue:
    """Normalized issue record (SPEC §4.1.1).

    Used by orchestration, prompt rendering, and observability output. Labels are
    expected to be normalized to lowercase at the tracker boundary (SPEC §11.3);
    derived identifiers are exposed via :attr:`workspace_key` and
    :attr:`normalized_state` per SPEC §4.2.

    Attributes:
        id: Stable tracker-internal ID; used for lookups and internal map keys.
        identifier: Human-readable ticket key (for example ``ABC-123``); used for
            logs and workspace naming.
        title: Issue title.
        state: Current tracker state name.
        description: Issue body, or ``None``.
        priority: Priority integer, or ``None``. Lower numbers are higher
            priority in dispatch sorting.
        branch_name: Tracker-provided branch metadata, or ``None``.
        url: Tracker URL for the issue, or ``None``.
        labels: Labels normalized to lowercase.
        blocked_by: Blocker references for issues that block this one.
        created_at: Creation timestamp, or ``None``.
        updated_at: Last-update timestamp, or ``None``.
    """

    id: str
    identifier: str
    title: str
    state: str
    description: str | None = None
    priority: int | None = None
    branch_name: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[BlockerRef] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def workspace_key(self) -> str:
        """Filesystem-safe workspace directory name for this issue (SPEC §4.2)."""
        return sanitize_workspace_key(self.identifier)

    @property
    def normalized_state(self) -> str:
        """This issue's state lowercased for comparison (SPEC §4.2)."""
        return normalize_state(self.state)
