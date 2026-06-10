"""Tests for the Linear GraphQL transport + error mapping (SPEC §11.2, §11.4)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from symphony.exceptions import (
    LinearApiRequestError,
    LinearApiStatusError,
    LinearGraphQLError,
    LinearUnknownPayloadError,
)
from symphony.linear_transport import LinearTransport

Handler = Callable[[httpx.Request], httpx.Response]


def _transport(handler: Handler, *, api_key: str = "lin_key") -> LinearTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LinearTransport("https://api.linear.app/graphql", api_key, client=client)


# --- request shaping (§11.2) --------------------------------------------------
def test_sends_auth_header_and_graphql_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"ok": True}})

    transport = _transport(handler, api_key="lin_secret")
    data = transport.execute("query Q { ok }", {"slug": "team"})

    assert data == {"ok": True}
    assert captured["auth"] == "lin_secret"
    assert captured["body"] == {
        "query": "query Q { ok }",
        "variables": {"slug": "team"},
    }


def test_variables_default_to_empty_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["variables"] == {}
        return httpx.Response(200, json={"data": {}})

    assert _transport(handler).execute("query { ok }") == {}


# --- error mapping (§11.4) ----------------------------------------------------
def test_transport_failure_maps_to_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LinearApiRequestError):
        _transport(handler).execute("query { ok }")


def test_timeout_maps_to_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(LinearApiRequestError):
        _transport(handler).execute("query { ok }")


def test_non_200_maps_to_status_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"data": {}})

    with pytest.raises(LinearApiStatusError):
        _transport(handler).execute("query { ok }")


def test_graphql_errors_map_to_graphql_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errors": [{"message": "bad query"}], "data": None}
        )

    with pytest.raises(LinearGraphQLError):
        _transport(handler).execute("query { ok }")


def test_invalid_json_maps_to_unknown_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
        )

    with pytest.raises(LinearUnknownPayloadError):
        _transport(handler).execute("query { ok }")


def test_missing_data_object_maps_to_unknown_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None})

    with pytest.raises(LinearUnknownPayloadError):
        _transport(handler).execute("query { ok }")


def test_non_object_body_maps_to_unknown_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    with pytest.raises(LinearUnknownPayloadError):
        _transport(handler).execute("query { ok }")


# --- client lifecycle ---------------------------------------------------------
def test_close_leaves_injected_client_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with LinearTransport("https://api", "k", client=client) as transport:
        assert transport.execute("query { ok }") == {}
    # An injected client is not owned, so close() must leave it open for the caller.
    assert client.is_closed is False
    client.close()


def test_close_closes_owned_client() -> None:
    # With no injected client the transport creates and owns one, and must close it.
    transport = LinearTransport("https://api.linear.app/graphql", "k")
    transport.close()
    assert transport._client.is_closed is True
