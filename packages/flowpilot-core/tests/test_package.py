"""Smoke test for the core package."""

import flowpilot_core


def test_version_is_set() -> None:
    assert flowpilot_core.__version__
