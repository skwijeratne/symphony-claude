"""Linear tracker operations and pagination (SPEC §11.1, §11.2).

Builds the three REQUIRED tracker operations (SPEC §11.1) on top of the
operation-agnostic :class:`~symphony.linear_transport.LinearTransport` and the
:func:`~symphony.linear_normalize.normalize_issue` normalizer:

* :meth:`LinearClient.fetch_candidate_issues` — issues in the configured active
  states for the configured project (the dispatch candidates).
* :meth:`LinearClient.fetch_issues_by_states` — issues in arbitrary states (startup
  terminal cleanup).
* :meth:`LinearClient.fetch_issue_states_by_ids` — issues by GraphQL ID, for
  active-run reconciliation.

This is where query construction lives, kept isolated per SPEC §11.2 so the exact
fields/types are testable in one place: the project filter uses
``project: { slugId: { eq: $slug } }``, queries select issue labels, the by-ID
refresh types its variable as ``[ID!]``, and candidate/by-state fetches paginate
(page size 50) preserving order across pages. Required-label filtering is *not* done
here — it happens after normalization in the orchestrator so reconciliation can
observe label removal (SPEC §11.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from symphony.exceptions import LinearMissingEndCursorError, LinearUnknownPayloadError
from symphony.linear_normalize import normalize_issue
from symphony.models import Issue

__all__ = ["GraphQLExecutor", "LinearClient"]

# SPEC §11.2: default candidate page size.
_PAGE_SIZE = 50

# The issue fields every operation selects. Shared so candidate, by-state, and
# by-ID queries normalize through the same path (SPEC §11.3 shape). Kept in sync
# with the raw shape documented in ``linear_normalize``.
_ISSUE_FIELDS = """
    id
    identifier
    title
    description
    priority
    branchName
    url
    createdAt
    updatedAt
    state { name }
    labels { nodes { name } }
    inverseRelations { nodes { type issue { id identifier state { name } } } }
"""

# Candidate / by-state fetch: project-scoped, filtered by a list of state names,
# paginated (SPEC §11.2). ``slugId`` is the project filter field required by §11.2.
_ISSUES_BY_STATE_QUERY = f"""
query IssuesByState(
  $slug: String!, $states: [String!], $first: Int!, $after: String
) {{
  issues(
    filter: {{
      project: {{ slugId: {{ eq: $slug }} }}
      state: {{ name: {{ in: $states }} }}
    }}
    first: $first
    after: $after
  ) {{
    nodes {{{_ISSUE_FIELDS}}}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""

# Issue-state refresh by ID for reconciliation. The ``$ids`` variable is typed
# ``[ID!]`` exactly as required by SPEC §11.2.
_ISSUE_STATES_BY_IDS_QUERY = f"""
query IssueStatesByIds($ids: [ID!], $first: Int!, $after: String) {{
  issues(filter: {{ id: {{ in: $ids }} }}, first: $first, after: $after) {{
    nodes {{{_ISSUE_FIELDS}}}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""


class GraphQLExecutor(Protocol):
    """Minimal transport contract the client depends on (see ``LinearTransport``)."""

    def execute(
        self, query: str, variables: Mapping[str, Any] | None = None
    ) -> dict[str, Any]: ...


class LinearClient:
    """The three required Linear tracker operations (SPEC §11.1).

    Args:
        transport: A GraphQL executor (typically a
            :class:`~symphony.linear_transport.LinearTransport`).
        project_slug: The Linear project ``slugId`` to scope queries to (SPEC §11.2).
        active_states: The configured active state names used for candidate fetch.
    """

    def __init__(
        self,
        transport: GraphQLExecutor,
        project_slug: str,
        active_states: Sequence[str],
    ) -> None:
        self._transport = transport
        self._project_slug = project_slug
        self._active_states = list(active_states)

    def fetch_candidate_issues(self) -> list[Issue]:
        """Fetch issues in the configured active states for the project (SPEC §11.1)."""
        return self._fetch_issues_in_states(self._active_states)

    def fetch_issues_by_states(self, state_names: Sequence[str]) -> list[Issue]:
        """Fetch issues in the given states (startup terminal cleanup, SPEC §11.1).

        An empty ``state_names`` returns ``[]`` without hitting the API (SPEC §17.3).
        """
        return self._fetch_issues_in_states(state_names)

    def fetch_issue_states_by_ids(self, issue_ids: Sequence[str]) -> list[Issue]:
        """Fetch issues by GraphQL ID for reconciliation (SPEC §11.1, §11.2).

        An empty ``issue_ids`` returns ``[]`` without hitting the API.
        """
        if not issue_ids:
            return []
        nodes = self._collect_nodes(
            _ISSUE_STATES_BY_IDS_QUERY, {"ids": list(issue_ids)}
        )
        return [normalize_issue(node) for node in nodes]

    def _fetch_issues_in_states(self, states: Sequence[str]) -> list[Issue]:
        if not states:
            return []
        nodes = self._collect_nodes(
            _ISSUES_BY_STATE_QUERY,
            {"slug": self._project_slug, "states": list(states)},
        )
        return [normalize_issue(node) for node in nodes]

    def _collect_nodes(
        self, query: str, variables: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Run ``query`` across all pages, returning the issue nodes in order.

        Follows the ``issues`` connection's ``pageInfo`` until exhausted (SPEC §11.2
        pagination). A ``hasNextPage`` with no ``endCursor`` is a pagination
        integrity error (SPEC §11.4).
        """
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page_variables = {**variables, "first": _PAGE_SIZE, "after": cursor}
            data = self._transport.execute(query, page_variables)
            connection = data.get("issues")
            if not isinstance(connection, Mapping):
                raise LinearUnknownPayloadError(
                    "Linear response is missing an 'issues' connection"
                )
            page_nodes = connection.get("nodes")
            if not isinstance(page_nodes, list):
                raise LinearUnknownPayloadError("Linear 'issues.nodes' is not a list")
            nodes.extend(page_nodes)

            page_info = connection.get("pageInfo")
            page_info = page_info if isinstance(page_info, Mapping) else {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                raise LinearMissingEndCursorError(
                    "Linear reported hasNextPage but no endCursor"
                )
        return nodes
