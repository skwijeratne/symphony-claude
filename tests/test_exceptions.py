"""Tests for the Symphony exception hierarchy (M0, PR #1).

These assert the *structure* of the hierarchy: every exported error is a
:class:`SymphonyError`, codes are stable/unique, category bases group the right
leaves, and message defaulting works. No behavior is exercised here.
"""

from __future__ import annotations

import pytest

from symphony import exceptions
from symphony.exceptions import (
    AgentSessionError,
    LinearApiStatusError,
    MissingWorkflowFileError,
    ObservabilityError,
    SymphonyError,
    TrackerError,
    WorkflowConfigError,
    WorkspaceError,
)

# Every public name in exceptions.__all__ except the base must be a SymphonyError.
EXPORTED_ERRORS = [getattr(exceptions, name) for name in exceptions.__all__]

# The five top-level failure-class bases (SPEC §14.1).
CATEGORY_BASES = (
    WorkflowConfigError,
    WorkspaceError,
    AgentSessionError,
    TrackerError,
    ObservabilityError,
)


def test_all_exports_resolve():
    # __all__ entries must all be importable attributes.
    for name in exceptions.__all__:
        assert hasattr(exceptions, name), name


def test_everything_derives_from_symphony_error():
    for err in EXPORTED_ERRORS:
        assert issubclass(err, SymphonyError), err


def test_category_bases_derive_directly_from_base():
    for base in CATEGORY_BASES:
        assert SymphonyError in base.__bases__, base


def test_every_leaf_belongs_to_a_category():
    leaves = [
        err
        for err in EXPORTED_ERRORS
        if err is not SymphonyError and err not in CATEGORY_BASES
    ]
    assert leaves  # sanity: there are leaf exceptions
    for leaf in leaves:
        assert issubclass(leaf, CATEGORY_BASES), leaf


def test_codes_are_non_empty_strings():
    for err in EXPORTED_ERRORS:
        assert isinstance(err.code, str) and err.code, err


def test_codes_are_unique():
    codes = [err.code for err in EXPORTED_ERRORS]
    duplicates = {code for code in codes if codes.count(code) > 1}
    assert not duplicates, f"duplicate codes: {duplicates}"


def test_spec_codes_present():
    # A representative spot-check of stable codes pinned by SPEC.md.
    expected = {
        "missing_workflow_file",
        "workflow_parse_error",
        "workflow_front_matter_not_a_map",
        "template_parse_error",
        "template_render_error",
        "claude_not_found",
        "startup_timeout",
        "turn_timeout",
        "max_turns",
        "max_budget",
        "unsupported_tracker_kind",
        "missing_tracker_api_key",
        "missing_tracker_project_slug",
        "linear_graphql_errors",
        "linear_missing_end_cursor",
    }
    actual = {err.code for err in EXPORTED_ERRORS}
    assert expected <= actual


def test_message_defaults_to_code():
    err = MissingWorkflowFileError()
    assert err.message == "missing_workflow_file"
    assert str(err) == "missing_workflow_file"


def test_message_can_be_overridden():
    err = LinearApiStatusError("HTTP 503 from Linear")
    assert err.message == "HTTP 503 from Linear"
    assert str(err) == "HTTP 503 from Linear"
    assert err.code == "linear_api_status"


def test_can_catch_by_category():
    with pytest.raises(WorkflowConfigError):
        raise MissingWorkflowFileError()
    with pytest.raises(SymphonyError):
        raise MissingWorkflowFileError()
