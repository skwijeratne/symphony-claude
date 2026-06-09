"""Tests for dispatch preflight validation (SPEC §6.3)."""

from __future__ import annotations

import pytest

from symphony.config import ClaudeConfig, ServiceConfig, TrackerConfig
from symphony.exceptions import DispatchPreflightError
from symphony.preflight import check_dispatch_preflight, ensure_dispatchable


def _config(
    *, tracker: TrackerConfig | None = None, claude: ClaudeConfig | None = None
) -> ServiceConfig:
    return ServiceConfig(
        tracker=tracker or TrackerConfig(api_key="lin_secret", project_slug="my-team"),
        claude=claude or ClaudeConfig(),
    )


def _codes(config: ServiceConfig) -> set[str]:
    return {problem.code for problem in check_dispatch_preflight(config)}


def test_valid_config_has_no_problems():
    assert check_dispatch_preflight(_config()) == []


def test_missing_api_key_is_flagged():
    cfg = _config(tracker=TrackerConfig(project_slug="my-team"))
    assert _codes(cfg) == {"missing_tracker_api_key"}


def test_empty_api_key_is_flagged():
    cfg = _config(tracker=TrackerConfig(api_key="", project_slug="my-team"))
    assert "missing_tracker_api_key" in _codes(cfg)


def test_missing_project_slug_is_flagged_for_linear():
    cfg = _config(tracker=TrackerConfig(api_key="lin_secret"))
    assert _codes(cfg) == {"missing_tracker_project_slug"}


def test_unsupported_kind_is_flagged():
    cfg = _config(
        tracker=TrackerConfig(kind="jira", api_key="x", project_slug="my-team")
    )
    assert _codes(cfg) == {"unsupported_tracker_kind"}


def test_empty_kind_is_flagged():
    cfg = _config(tracker=TrackerConfig(kind="", api_key="x", project_slug="my-team"))
    assert _codes(cfg) == {"unsupported_tracker_kind"}


def test_project_slug_not_required_for_non_linear_kind():
    # An unsupported kind is flagged, but the linear-only slug check does not fire.
    cfg = _config(tracker=TrackerConfig(kind="jira", api_key="x"))
    assert _codes(cfg) == {"unsupported_tracker_kind"}


def test_kind_match_is_case_insensitive():
    cfg = _config(
        tracker=TrackerConfig(kind="Linear", api_key="x", project_slug="my-team")
    )
    assert check_dispatch_preflight(cfg) == []


def test_empty_claude_command_is_flagged():
    cfg = _config(claude=ClaudeConfig(command="   "))
    assert _codes(cfg) == {"missing_claude_command"}


def test_multiple_problems_are_collected_together():
    cfg = ServiceConfig(
        tracker=TrackerConfig(kind="", api_key=None, project_slug=None),
        claude=ClaudeConfig(command=""),
    )
    assert _codes(cfg) == {
        "unsupported_tracker_kind",
        "missing_tracker_api_key",
        "missing_claude_command",
    }


def test_ensure_dispatchable_passes_for_valid_config():
    ensure_dispatchable(_config())  # does not raise


def test_ensure_dispatchable_raises_with_problem_codes():
    cfg = _config(tracker=TrackerConfig(project_slug="my-team"))
    with pytest.raises(DispatchPreflightError) as exc:
        ensure_dispatchable(cfg)
    assert exc.value.code == "dispatch_preflight_failed"
    assert exc.value.problem_codes == ["missing_tracker_api_key"]
