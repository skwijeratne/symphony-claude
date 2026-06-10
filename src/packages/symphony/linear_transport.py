"""Linear GraphQL transport and error mapping (SPEC §11.2, §11.4).

A thin, operation-agnostic client that executes a single GraphQL operation against
the Linear endpoint and returns its ``data`` object. It owns the wire concerns the
spec pins down — the ``Authorization`` header, a JSON ``{query, variables}`` body,
and a 30s network timeout — plus the §11.4 error mapping:

* transport failure (connection error, timeout) -> :class:`LinearApiRequestError`
* non-200 HTTP status -> :class:`LinearApiStatusError`
* top-level GraphQL ``errors`` -> :class:`LinearGraphQLError`
* unparseable / unexpectedly-shaped body -> :class:`LinearUnknownPayloadError`

Query construction for the three required operations and pagination is a separate
concern (SPEC §11.1, a later PR); this layer is deliberately ignorant of issue
shapes so the exact query fields/types can be tested where they are built (SPEC
§11.2: "keep query construction isolated").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from symphony.exceptions import (
    LinearApiRequestError,
    LinearApiStatusError,
    LinearGraphQLError,
    LinearUnknownPayloadError,
)

__all__ = ["LinearTransport"]

# SPEC §11.2: Linear network timeout is 30000 ms.
_DEFAULT_TIMEOUT_MS = 30000
_MS_PER_S = 1000


class LinearTransport:
    """Executes GraphQL operations against a Linear-compatible endpoint.

    Args:
        endpoint: The GraphQL endpoint URL (SPEC §6.4 default
            ``https://api.linear.app/graphql``).
        api_key: The resolved tracker API key, sent verbatim in the
            ``Authorization`` header (Linear personal API key convention).
        timeout_ms: Network timeout in milliseconds (SPEC §11.2 default 30000).
        client: An optional pre-built :class:`httpx.Client`, primarily a testing
            seam (inject one backed by :class:`httpx.MockTransport`). When omitted,
            a client is created with the configured timeout and owned by this
            instance.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
        client: httpx.Client | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._timeout_s = timeout_ms / _MS_PER_S
        # Authorization carries the raw key (Linear personal API keys are not
        # prefixed with "Bearer"). Sent per-request so an injected client need not
        # be pre-configured with auth.
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        self._client = client if client is not None else httpx.Client()
        self._owns_client = client is None

    def execute(
        self, query: str, variables: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute one GraphQL operation and return its ``data`` object.

        Args:
            query: A single GraphQL query/mutation document.
            variables: GraphQL variables, or ``None`` for an empty variable set.

        Returns:
            The ``data`` object from the GraphQL response.

        Raises:
            LinearApiRequestError: The request failed at the transport level
                (connection error or timeout).
            LinearApiStatusError: The endpoint returned a non-200 HTTP status.
            LinearGraphQLError: The response carried top-level GraphQL ``errors``.
            LinearUnknownPayloadError: The body was not JSON, or lacked a ``data``
                object.
        """
        payload = {"query": query, "variables": dict(variables or {})}
        try:
            response = self._client.post(
                self._endpoint,
                json=payload,
                headers=self._headers,
                timeout=self._timeout_s,
            )
        except httpx.RequestError as exc:
            raise LinearApiRequestError(
                f"Linear request to {self._endpoint} failed: {exc}"
            ) from exc

        if response.status_code != httpx.codes.OK:
            raise LinearApiStatusError(f"Linear returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise LinearUnknownPayloadError(
                "Linear response body was not valid JSON"
            ) from exc

        if not isinstance(body, dict):
            raise LinearUnknownPayloadError(
                f"Linear response was not a JSON object: {type(body).__name__}"
            )

        errors = body.get("errors")
        if errors:
            raise LinearGraphQLError(f"Linear GraphQL errors: {errors}")

        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearUnknownPayloadError(
                "Linear response is missing a 'data' object"
            )
        return data

    def close(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LinearTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
