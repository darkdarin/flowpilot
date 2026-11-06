# flowpilot-soniox

[Soniox](https://soniox.com) nodes for [FlowPilot](https://github.com/darkdarin/flowpilot), talking to
the Soniox real-time WebSocket API directly:

- `soniox.stt` — streaming speech-to-text with partial results, its own end-of-speech detection,
  context (terms, text) and wake word removal;
- `soniox.tts` — streaming text-to-speech: speaks while the agent is still writing.

Both need a Soniox API key, kept as a runtime secret (`{"secret": "soniox"}`).
