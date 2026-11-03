"""Smoke test for the SDK package."""

import flowpilot_sdk


def test_version_is_set() -> None:
    assert flowpilot_sdk.__version__
