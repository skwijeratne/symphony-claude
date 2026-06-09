"""Tests for the normalization rules (SPEC §4.2, §4.1.1)."""

from __future__ import annotations

import pytest

from symphony.normalization import (
    normalize_label,
    normalize_labels,
    normalize_state,
    sanitize_workspace_key,
    states_equal,
)


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("ABC-123", "ABC-123"),  # already safe: letters, digits, hyphen
        ("a.b_c-1", "a.b_c-1"),  # dot, underscore, hyphen all preserved
        ("feature/login", "feature_login"),  # slash replaced
        ("ENG 42", "ENG_42"),  # space replaced
        ("issue#1@host", "issue_1_host"),  # multiple disallowed chars
        ("café", "caf_"),  # non-ASCII replaced
        ("a/b\\c:d", "a_b_c_d"),  # several path separators
        ("", ""),  # empty stays empty
    ],
)
def test_sanitize_workspace_key(identifier, expected):
    assert sanitize_workspace_key(identifier) == expected


def test_sanitize_workspace_key_is_idempotent():
    once = sanitize_workspace_key("a/b c#d")
    assert sanitize_workspace_key(once) == once


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("In Progress", "in progress"),
        ("DONE", "done"),
        ("todo", "todo"),
        ("Human Review", "human review"),
    ],
)
def test_normalize_state(state, expected):
    assert normalize_state(state) == expected


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        ("Done", "done", True),
        ("In Progress", "IN PROGRESS", True),
        ("Todo", "Done", False),
    ],
)
def test_states_equal(left, right, equal):
    assert states_equal(left, right) is equal


def test_normalize_label():
    assert normalize_label("Bug") == "bug"


def test_normalize_labels_lowercases_and_preserves_order():
    assert normalize_labels(["Bug", "P1", "Needs-Review"]) == [
        "bug",
        "p1",
        "needs-review",
    ]


def test_normalize_labels_keeps_duplicates():
    assert normalize_labels(["A", "a", "A"]) == ["a", "a", "a"]


def test_normalize_labels_empty():
    assert normalize_labels([]) == []
