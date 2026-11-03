# flowpilot-vad

`vad.segmenter` passes audio through unchanged (no added latency for the speech-to-text node behind
it) and ends the stream when the voice command is over. Put it in front of speech-to-text services
that need an external voice activity detector, such as Whisper; services that detect the end of
speech themselves (e.g. Soniox) do not need it.

The end-of-command logic follows Home Assistant's `VoiceCommandSegmenter`. Detectors:

- `micro` (default): `pymicro-vad`, the same detector the Home Assistant Assist pipeline uses;
- `energy`: loudness threshold, deterministic and dependency-free (tests, fallback).
