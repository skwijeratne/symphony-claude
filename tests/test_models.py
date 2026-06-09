"""Tests for the core domain models (SPEC §4.1.1, §4.2)."""

from __future__ import annotations

from datetime import datetime

from symphony.models import BlockerRef, Issue


def _issue(**overrides) -> Issue:
    base = {"id": "iss_1", "identifier": "ABC-123", "title": "Title", "state": "Todo"}
    base.update(overrides)
    return Issue(**base)


def test_issue_defaults():
    issue = _issue()
    assert issue.description is None
    assert issue.priority is None
    assert issue.branch_name is None
    assert issue.url is None
    assert issue.labels == []
    assert issue.blocked_by == []
    assert issue.created_at is None
    assert issue.updated_at is None


def test_issue_default_collections_are_independent():
    a = _issue()
    b = _issue()
    a.labels.append("bug")
    a.blocked_by.append(BlockerRef(id="x"))
    assert b.labels == []
    assert b.blocked_by == []


def test_issue_full_construction():
    created = datetime(2026, 1, 1)
    updated = datetime(2026, 1, 2)
    blocker = BlockerRef(id="iss_0", identifier="ABC-1", state="In Progress")
    issue = _issue(
        description="body",
        priority=2,
        branch_name="feature/abc-123",
        url="https://tracker/ABC-123",
        labels=["bug"],
        blocked_by=[blocker],
        created_at=created,
        updated_at=updated,
    )
    assert issue.priority == 2
    assert issue.blocked_by == [blocker]
    assert issue.created_at == created
    assert issue.updated_at == updated


def test_workspace_key_derives_from_identifier():
    assert _issue(identifier="feature/login").workspace_key == "feature_login"
    assert _issue(identifier="ABC-123").workspace_key == "ABC-123"


def test_normalized_state_lowercases():
    assert _issue(state="In Progress").normalized_state == "in progress"


def test_blocker_ref_defaults():
    ref = BlockerRef()
    assert ref.id is None
    assert ref.identifier is None
    assert ref.state is None
