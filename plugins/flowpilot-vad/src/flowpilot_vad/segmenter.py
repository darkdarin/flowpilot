"""End-of-command detection from per-frame speech probabilities.

The state machine follows Home Assistant's ``VoiceCommandSegmenter``
(homeassistant/components/assist_pipeline/vad.py, Apache-2.0) so commands end the way Assist users
are used to.
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

FRAME_S = 0.01
FRAME_BYTES = 320  # 10 ms of 16 kHz mono s16le


class Outcome(StrEnum):
    CONTINUE = "continue"
    END = "end"  # the voice command finished
    TIMEOUT = "timeout"  # gave up waiting for the end of the command


@dataclass
class CommandSegmenter:
    speech_s: float = 0.3
    """Speech needed before a command counts as started."""

    command_s: float = 1.0
    """Minimum length of a command."""

    silence_s: float = 0.7
    """Silence that ends a started command."""

    timeout_s: float = 15.0
    """Hard limit for the whole command."""

    reset_s: float = 1.0
    """Silence before a command that resets the speech counter (or speech that resets silence)."""

    before_threshold: float = 0.2
    in_command_threshold: float = 0.5

    in_command: bool = False
    _speech_left: float = field(init=False, default=0.0)
    _command_left: float = field(init=False, default=0.0)
    _silence_left: float = field(init=False, default=0.0)
    _timeout_left: float = field(init=False, default=0.0)
    _reset_left: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.in_command = False
        self._speech_left = self.speech_s
        self._command_left = self.command_s - self.speech_s
        self._silence_left = self.silence_s
        self._timeout_left = self.timeout_s
        self._reset_left = self.reset_s

    def process(self, frame_s: float, probability: float) -> Outcome:
        self._timeout_left -= frame_s
        if self._timeout_left <= 0:
            return Outcome.TIMEOUT
        if not self.in_command:
            if probability > self.before_threshold:
                self._reset_left = self.reset_s
                self._speech_left -= frame_s
                if self._speech_left <= 0:
                    self.in_command = True
                    self._command_left = self.command_s - self.speech_s
                    self._silence_left = self.silence_s
            else:
                self._reset_left -= frame_s
                if self._reset_left <= 0:
                    self._speech_left = self.speech_s
                    self._reset_left = self.reset_s
        elif probability > self.in_command_threshold:
            self._reset_left -= frame_s
            self._command_left -= frame_s
            if self._reset_left <= 0:
                self._silence_left = self.silence_s
                self._reset_left = self.reset_s
        else:
            self._reset_left = self.reset_s
            self._silence_left -= frame_s
            self._command_left -= frame_s
            if self._silence_left <= 0 and self._command_left <= 0:
                return Outcome.END
        return Outcome.CONTINUE


class Detector(Protocol):
    """Speech probability for one 10 ms frame of 16 kHz mono s16le audio."""

    def probability(self, frame: bytes) -> float: ...


class MicroDetector:
    """``pymicro-vad``, the Assist pipeline detector. Negative results mean "not enough audio"."""

    def __init__(self) -> None:
        from pymicro_vad import MicroVad

        self._vad = MicroVad()

    def probability(self, frame: bytes) -> float:
        return max(0.0, float(self._vad.Process10ms(frame)))


class EnergyDetector:
    """Loudness-based: probability rises from 0 to 1 between ``floor_dbfs`` and ``speech_dbfs``."""

    def __init__(self, speech_dbfs: float = -35.0, floor_dbfs: float = -50.0) -> None:
        self._speech = speech_dbfs
        self._floor = floor_dbfs

    def probability(self, frame: bytes) -> float:
        samples = array.array("h", frame)
        if not samples:
            return 0.0
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        dbfs = 20 * math.log10(rms / 32768) if rms > 0 else -120.0
        if dbfs <= self._floor:
            return 0.0
        if dbfs >= self._speech:
            return 1.0
        return (dbfs - self._floor) / (self._speech - self._floor)


class FrameBuffer:
    """Re-cuts arbitrary chunks into fixed 10 ms frames."""

    def __init__(self) -> None:
        self._pending = b""

    def frames(self, chunk: bytes) -> list[bytes]:
        data = self._pending + chunk
        cut = len(data) - len(data) % FRAME_BYTES
        self._pending = data[cut:]
        return [data[i : i + FRAME_BYTES] for i in range(0, cut, FRAME_BYTES)]
