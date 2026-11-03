"""Version range matching."""

import pytest

from flowpilot_core.versions import satisfies, validate_range


@pytest.mark.parametrize(
    ("version", "range_", "expected"),
    [
        ("1.2.3", "*", True),
        ("1.2.3", "1.2.3", True),
        ("1.2.4", "1.2.3", False),
        ("1.9.0", "^1", True),
        ("2.0.0", "^1", False),
        ("1.2.0", "^1.2", True),
        ("1.1.9", "^1.2", False),
        ("1.2.5", "^1.2.3", True),
        ("0.2.5", "^0.2", True),
        ("0.3.0", "^0.2", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("1.2.9", "~1.2", True),
        ("1.3.0", "~1.2", False),
        ("1.2.3", "~1.2.3", True),
    ],
)
def test_satisfies(version: str, range_: str, expected: bool) -> None:
    assert satisfies(version, range_) is expected


@pytest.mark.parametrize("bad", ["latest", "1.2", "~1", "^01", ">=1.0.0", ""])
def test_invalid_ranges(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_range(bad)
