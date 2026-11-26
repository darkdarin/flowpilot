# flowpilot-soniox

[Soniox](https://soniox.com) nodes for [FlowPilot](https://github.com/darkdarin/flowpilot), talking to
the Soniox real-time WebSocket API directly:

- `soniox.stt` — streaming speech-to-text with partial results, its own end-of-speech detection,
  language detection (the turn's language follows what was said), context (terms, text) and wake
  word removal. A dialogue keeps one connection between its commands (30 s after each by
  default; Soniox bills the time it is open), and Soniox is told the assistant's last answer, so
  it knows what a short reply answers and leaves the assistant's echo out of the text;
- `soniox.tts` — streaming text-to-speech: speaks while the agent is still writing, in the language
  of the answer unless the settings fix one; cloned voices too.

Both need a Soniox API key, kept as a runtime secret (`{"secret": "soniox"}`).
