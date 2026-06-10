"""Tests for Linear issue normalization (SPEC §11.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from symphony.exceptions import LinearUnknownPayloadError
from symphony.linear_normalize import normalize_issue


def _node(**overrides: Any) -> dict[str, Any]:
    """A complete, valid raw Linear issue node; override fields per test."""
    node: dict[str, Any] = {
        "id": "uuid-1",
        "identifier": "ABC-123",
        "title": "Fix the thing",
        "state": {"name": "Todo"},
        "description": "body",
        "priority": 2,
        "branchName": "abc-123-fix",
        "url": "https://linear.app/abc/issue/ABC-123",
        "createdAt": "2024-01-15T10:30:00.000Z",
        "updatedAt": "2024-02-01T08:00:00+00:00",
        "labels": {"nodes": [{"name": "Bug"}, {"name": "  Backend "}]},
        "inverseRelations": {
            "nodes": [
                {
                    "type": "blocks",
                    "issue": {
                        "id": "uuid-2",
                        "identifier": "ABC-1",
                        "state": {"name": "In Progress"},
                    },
                },
                {"type": "related", "issue": {"id": "uuid-9"}},
            ]
        },
    }
    node.update(overrides)
    return node


def test_full_node_maps_all_fields() -> None:
    issue = normalize_issue(_node())
    assert issue.id == "uuid-1"
    assert issue.identifier == "ABC-123"
    assert issue.title == "Fix the thing"
    assert issue.state == "Todo"
    assert issue.description == "body"
    assert issue.priority == 2
    assert issue.branch_name == "abc-123-fix"
    assert issue.url == "https://linear.app/abc/issue/ABC-123"


def test_labels_are_trimmed_and_lowercased() -> None:
    assert normalize_issue(_node()).labels == ["bug", "backend"]


def test_blockers_use_only_blocks_relations() -> None:
    blockers = normalize_issue(_node()).blocked_by
    assert len(blockers) == 1
    assert blockers[0].id == "uuid-2"
    assert blockers[0].identifier == "ABC-1"
    assert blockers[0].state == "In Progress"


def test_timestamps_are_parsed() -> None:
    issue = normalize_issue(_node())
    assert isinstance(issue.created_at, datetime)
    assert issue.created_at.year == 2024 and issue.created_at.month == 1
    assert isinstance(issue.updated_at, datetime)


@pytest.mark.parametrize(
    "value, expected",
    [
        (2, 2),
        (0, 0),
        (True, None),
        (False, None),
        (1.5, None),
        ("3", None),
        (None, None),
    ],
)
def test_priority_is_integer_only(value: Any, expected: int | None) -> None:
    assert normalize_issue(_node(priority=value)).priority == expected


def test_minimal_node_uses_defaults() -> None:
    minimal = {
        "id": "i",
        "identifier": "ABC-9",
        "title": "t",
        "state": {"name": "Todo"},
    }
    issue = normalize_issue(minimal)
    assert issue.description is None
    assert issue.priority is None
    assert issue.branch_name is None
    assert issue.url is None
    assert issue.labels == []
    assert issue.blocked_by == []
    assert issue.created_at is None
    assert issue.updated_at is None


def test_unparseable_timestamp_degrades_to_none() -> None:
    issue = normalize_issue(_node(createdAt="not-a-date", updatedAt=12345))
    assert issue.created_at is None
    assert issue.updated_at is None


def test_malformed_label_and_relation_nodes_are_skipped() -> None:
    node = _node(
        labels={"nodes": [{"name": "Keep"}, {"name": "  "}, {"noname": "x"}, "junk"]},
        inverseRelations={
            "nodes": [{"type": "blocks"}, "junk", {"type": "blocks", "issue": "x"}]
        },
    )
    issue = normalize_issue(node)
    assert issue.labels == ["keep"]
    assert issue.blocked_by == []


@pytest.mark.parametrize("missing", ["id", "identifier", "title"])
def test_missing_required_string_field_raises(missing: str) -> None:
    node = _node()
    del node[missing]
    with pytest.raises(LinearUnknownPayloadError):
        normalize_issue(node)


def test_missing_state_raises() -> None:
    node = _node()
    del node["state"]
    with pytest.raises(LinearUnknownPayloadError):
        normalize_issue(node)


def test_empty_required_field_raises() -> None:
    with pytest.raises(LinearUnknownPayloadError):
        normalize_issue(_node(identifier=""))


def test_non_mapping_node_raises() -> None:
    with pytest.raises(LinearUnknownPayloadError):
        normalize_issue("not a node")  # type: ignore[arg-type]
