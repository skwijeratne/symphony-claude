"""Normalize raw Linear issue payloads into the domain :class:`Issue` (SPEC §11.3).

Turns one Linear GraphQL issue node into the normalized record the orchestrator,
prompt renderer, and observability layer consume (SPEC §4.1.1). The normalization
rules (SPEC §11.3) are:

* ``labels`` -> label names trimmed and lowercased
* ``blocked_by`` -> derived from *inverse* relations whose type is ``blocks``
  (the issues that block this one)
* ``priority`` -> kept only when it is an integer; anything else becomes ``None``
* ``created_at`` / ``updated_at`` -> parsed from ISO-8601 strings

The expected raw shape (the fields the §11.1 queries will select, isolated per SPEC
§11.2) is::

    {
      "id": "...", "identifier": "ABC-123", "title": "...", "state": {"name": "Todo"},
      "description": "...", "priority": 2, "branchName": "...", "url": "...",
      "createdAt": "2024-01-15T10:30:00.000Z", "updatedAt": "...",
      "labels": {"nodes": [{"name": "Bug"}, ...]},
      "inverseRelations": {"nodes": [
          {"type": "blocks",
           "issue": {"id": "...", "identifier": "ABC-1", "state": {"name": "..."}}},
      ]},
    }

Reading is defensive: optional/absent fields normalize to ``None`` or empty lists,
but a missing core field (``id``/``identifier``/``title``/``state``) is treated as a
malformed payload (:class:`LinearUnknownPayloadError`, SPEC §11.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from symphony.exceptions import LinearUnknownPayloadError
from symphony.models import BlockerRef, Issue
from symphony.normalization import normalize_label

__all__ = ["normalize_issue"]

# Linear relation type that means "the related issue blocks this one" when seen on
# an issue's inverse relations (SPEC §11.3).
_BLOCKS_RELATION = "blocks"


def normalize_issue(node: Mapping[str, Any]) -> Issue:
    """Normalize one raw Linear issue node into an :class:`Issue` (SPEC §11.3).

    Args:
        node: A single Linear GraphQL issue node.

    Returns:
        The normalized issue.

    Raises:
        LinearUnknownPayloadError: A required field is missing or the node is not a
            mapping (SPEC §11.4).
    """
    if not isinstance(node, Mapping):
        raise LinearUnknownPayloadError(
            f"issue node is not an object: {type(node).__name__}"
        )
    return Issue(
        id=_require_str(node, "id"),
        identifier=_require_str(node, "identifier"),
        title=_require_str(node, "title"),
        state=_require_state_name(node),
        description=_opt_str(node.get("description")),
        priority=_normalize_priority(node.get("priority")),
        branch_name=_opt_str(node.get("branchName")),
        url=_opt_str(node.get("url")),
        labels=_normalize_labels(node.get("labels")),
        blocked_by=_normalize_blockers(node.get("inverseRelations")),
        created_at=_parse_timestamp(node.get("createdAt")),
        updated_at=_parse_timestamp(node.get("updatedAt")),
    )


def _require_str(node: Mapping[str, Any], key: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value:
        raise LinearUnknownPayloadError(f"issue node is missing required '{key}'")
    return value


def _require_state_name(node: Mapping[str, Any]) -> str:
    state = node.get("state")
    if not isinstance(state, Mapping):
        raise LinearUnknownPayloadError("issue node is missing required 'state'")
    return _require_str(state, "name")


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_priority(value: Any) -> int | None:
    """Keep ``value`` only when it is a true integer (SPEC §11.3).

    ``bool`` is excluded even though it subclasses ``int``; non-integers (floats,
    strings, ``None``) normalize to ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _normalize_labels(labels: Any) -> list[str]:
    """Extract label names, each trimmed and lowercased (SPEC §11.3)."""
    nodes = _connection_nodes(labels)
    names: list[str] = []
    for label in nodes:
        if not isinstance(label, Mapping):
            continue
        raw = label.get("name")
        if isinstance(raw, str) and raw.strip():
            names.append(normalize_label(raw.strip()))
    return names


def _normalize_blockers(inverse_relations: Any) -> list[BlockerRef]:
    """Derive blockers from inverse relations of type ``blocks`` (SPEC §11.3)."""
    blockers: list[BlockerRef] = []
    for relation in _connection_nodes(inverse_relations):
        if not isinstance(relation, Mapping):
            continue
        if relation.get("type") != _BLOCKS_RELATION:
            continue
        blocker = relation.get("issue")
        if not isinstance(blocker, Mapping):
            continue
        state = blocker.get("state")
        state_name = state.get("name") if isinstance(state, Mapping) else None
        blockers.append(
            BlockerRef(
                id=_opt_str(blocker.get("id")),
                identifier=_opt_str(blocker.get("identifier")),
                state=_opt_str(state_name),
            )
        )
    return blockers


def _connection_nodes(connection: Any) -> list[Any]:
    """Return the ``nodes`` list of a GraphQL connection, or empty if absent."""
    if isinstance(connection, Mapping):
        nodes = connection.get("nodes")
        if isinstance(nodes, list):
            return nodes
    return []


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string, or ``None`` if absent/unparseable.

    Timestamps are non-critical optional fields (SPEC §4.1.1), so a malformed value
    degrades to ``None`` rather than failing the whole issue.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
