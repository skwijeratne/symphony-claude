"""Stable-identifier and value normalization rules (SPEC §4.2).

These are the pure, side-effect-free transforms that the rest of Symphony relies
on to derive workspace directory names, compare tracker states, and normalize
issue labels. They are defined once here so the tracker layer, workspace
manager, and orchestrator all apply identical rules.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "sanitize_workspace_key",
    "normalize_state",
    "normalize_label",
    "normalize_labels",
    "states_equal",
]

# Characters permitted verbatim in a workspace directory name (SPEC §4.2).
# Anything outside this set is replaced with a single underscore.
_WORKSPACE_KEY_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_workspace_key(identifier: str) -> str:
    """Derive a filesystem-safe workspace key from an issue identifier.

    Per SPEC §4.2, every character not in ``[A-Za-z0-9._-]`` is replaced with an
    underscore. The result is used as the workspace directory name.

    Args:
        identifier: The human-readable issue identifier (for example ``ABC-123``).

    Returns:
        The sanitized identifier safe for use as a directory name.
    """
    return _WORKSPACE_KEY_DISALLOWED.sub("_", identifier)


def normalize_state(state: str) -> str:
    """Normalize a tracker state name for comparison (SPEC §4.2).

    Args:
        state: A raw tracker state name (for example ``In Progress``).

    Returns:
        The lowercased state name.
    """
    return state.lower()


def states_equal(left: str, right: str) -> bool:
    """Compare two tracker state names using normalized equality (SPEC §4.2).

    Args:
        left: A raw tracker state name.
        right: A raw tracker state name.

    Returns:
        ``True`` if the states are equal after normalization.
    """
    return normalize_state(left) == normalize_state(right)


def normalize_label(label: str) -> str:
    """Normalize a single issue label to lowercase (SPEC §4.1.1).

    Args:
        label: A raw label string.

    Returns:
        The lowercased label.
    """
    return label.lower()


def normalize_labels(labels: Iterable[str]) -> list[str]:
    """Normalize a collection of issue labels to lowercase (SPEC §4.1.1).

    Order is preserved; duplicates are not removed.

    Args:
        labels: Raw label strings.

    Returns:
        A list of lowercased labels in the original order.
    """
    return [normalize_label(label) for label in labels]
