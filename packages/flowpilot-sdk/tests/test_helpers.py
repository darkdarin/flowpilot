"""Helpers for node implementations."""

from __future__ import annotations

import pytest

from flowpilot_sdk import helpers
from flowpilot_sdk.helpers import SpeechTiming


def test_speech_is_timed_from_the_first_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTS node waits for the agent first: that wait is not the synthesis's time."""
    now = [100.0]
    monkeypatch.setattr(f"{helpers.__name__}.time.monotonic", lambda: now[0])
    timing = SpeechTiming()
    now[0] = 103.5  # the agent's first words
    timing.text()
    now[0] = 103.6
    timing.text()  # later text does not move the start
    now[0] = 104.3
    timing.audio(24000, 24000)  # one second of speech
    now[0] = 105.5
    timing.audio(48000, 24000)
    assert timing.attrs() == {
        "first_audio_ms": 800,
        "done_ms": 2000,
        "seconds": 3.0,
        "realtime": 1.5,
    }


def test_no_audio_has_no_times() -> None:
    timing = SpeechTiming()
    timing.text()
    assert timing.attrs() == {
        "first_audio_ms": None,
        "done_ms": None,
        "seconds": 0.0,
        "realtime": None,
    }
