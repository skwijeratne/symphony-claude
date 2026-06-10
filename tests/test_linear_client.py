"""Tests for the Linear tracker operations + pagination (SPEC §11.1, §11.2, §17.3)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from symphony.exceptions import LinearMissingEndCursorError, LinearUnknownPayloadError
from symphony.linear_client import LinearClient


class FakeTransport:
    """Returns queued responses and records each (query, variables) call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self, query: str, variables: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((query, dict(variables or {})))
        return self._responses.pop(0)


def _issue_node(identifier: str, *, state: str = "Todo") -> dict[str, Any]:
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"Title {identifier}",
        "state": {"name": state},
        "labels": {"nodes": [{"name": "Bug"}]},
    }


def _page(
    *identifiers: str, has_next: bool = False, cursor: str | None = None
) -> dict[str, Any]:
    # Shaped like LinearTransport.execute's return value: the unwrapped `data`.
    return {
        "issues": {
            "nodes": [_issue_node(i) for i in identifiers],
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        }
    }


def _client(
    transport: FakeTransport, *, active: list[str] | None = None
) -> LinearClient:
    return LinearClient(transport, "my-team", active or ["Todo", "In Progress"])


# --- candidate fetch (§11.1, §11.2) -------------------------------------------
def test_candidate_fetch_filters_by_project_slug_and_active_states() -> None:
    transport = FakeTransport([_page("ABC-1", "ABC-2")])
    issues = _client(transport).fetch_candidate_issues()

    assert [issue.identifier for issue in issues] == ["ABC-1", "ABC-2"]
    query, variables = transport.calls[0]
    assert "slugId" in query
    assert variables["slug"] == "my-team"
    assert variables["states"] == ["Todo", "In Progress"]
    assert variables["first"] == 50
    assert variables["after"] is None


def test_candidate_fetch_normalizes_issues() -> None:
    transport = FakeTransport([_page("ABC-1")])
    issue = _client(transport).fetch_candidate_issues()[0]
    assert issue.labels == ["bug"]  # normalized to lowercase (§11.3)
    assert issue.state == "Todo"


# --- pagination (§11.2, §17.3) ------------------------------------------------
def test_pagination_collects_all_pages_in_order() -> None:
    transport = FakeTransport(
        [
            _page("ABC-1", "ABC-2", has_next=True, cursor="cur-1"),
            _page("ABC-3", has_next=False),
        ]
    )
    issues = _client(transport).fetch_candidate_issues()

    assert [issue.identifier for issue in issues] == ["ABC-1", "ABC-2", "ABC-3"]
    # The second page is requested with the first page's end cursor.
    assert transport.calls[1][1]["after"] == "cur-1"


def test_missing_end_cursor_with_next_page_raises() -> None:
    transport = FakeTransport([_page("ABC-1", has_next=True, cursor=None)])
    with pytest.raises(LinearMissingEndCursorError):
        _client(transport).fetch_candidate_issues()


# --- fetch_issues_by_states (§11.1, §17.3) ------------------------------------
def test_fetch_by_empty_states_makes_no_api_call() -> None:
    transport = FakeTransport([])
    assert _client(transport).fetch_issues_by_states([]) == []
    assert transport.calls == []


def test_fetch_by_states_uses_given_states() -> None:
    transport = FakeTransport([_page("ABC-9")])
    issues = _client(transport).fetch_issues_by_states(["Done", "Canceled"])
    assert [issue.identifier for issue in issues] == ["ABC-9"]
    assert transport.calls[0][1]["states"] == ["Done", "Canceled"]


# --- fetch_issue_states_by_ids (§11.1, §11.2) ---------------------------------
def test_fetch_by_empty_ids_makes_no_api_call() -> None:
    transport = FakeTransport([])
    assert _client(transport).fetch_issue_states_by_ids([]) == []
    assert transport.calls == []


def test_fetch_by_ids_uses_id_list_typing_and_returns_issues() -> None:
    transport = FakeTransport([_page("ABC-1", "ABC-2")])
    issues = _client(transport).fetch_issue_states_by_ids(["id-ABC-1", "id-ABC-2"])

    assert [issue.identifier for issue in issues] == ["ABC-1", "ABC-2"]
    query, variables = transport.calls[0]
    assert "[ID!]" in query  # §11.2 ID typing
    assert variables["ids"] == ["id-ABC-1", "id-ABC-2"]


def test_fetch_by_ids_paginates() -> None:
    transport = FakeTransport(
        [
            _page("ABC-1", has_next=True, cursor="cur-1"),
            _page("ABC-2", has_next=False),
        ]
    )
    issues = _client(transport).fetch_issue_states_by_ids(["a", "b"])
    assert [issue.identifier for issue in issues] == ["ABC-1", "ABC-2"]


# --- malformed payloads (§11.4) -----------------------------------------------
def test_missing_issues_connection_raises() -> None:
    transport = FakeTransport([{}])
    with pytest.raises(LinearUnknownPayloadError):
        _client(transport).fetch_candidate_issues()


def test_non_list_nodes_raises() -> None:
    transport = FakeTransport([{"issues": {"nodes": "oops"}}])
    with pytest.raises(LinearUnknownPayloadError):
        _client(transport).fetch_candidate_issues()


def test_absent_page_info_ends_pagination() -> None:
    # A connection with no pageInfo is treated as a single, final page.
    transport = FakeTransport([{"issues": {"nodes": [_issue_node("ABC-1")]}}])
    issues = _client(transport).fetch_candidate_issues()
    assert [issue.identifier for issue in issues] == ["ABC-1"]
