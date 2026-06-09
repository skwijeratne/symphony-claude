"""Smoke tests for the package skeleton (M0, PR #1)."""

from __future__ import annotations

import symphony


def test_version_is_exposed():
    assert symphony.__version__ == "0.1.0"


def test_version_in_all():
    assert "__version__" in symphony.__all__
