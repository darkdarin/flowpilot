# flowpilot-wyoming

FlowPilot nodes that act as Wyoming **clients**, so one node works with any Wyoming service:

- `wyoming.stt` — streams audio to a Wyoming ASR service (Whisper, wyoming-soniox, …). Emits partial
  transcripts when the service supports transcript streaming and stops sending audio as soon as the
  service returns the final transcript (services that detect the end of speech themselves).
- `wyoming.tts` — synthesizes the agent's streamed text. Uses Wyoming synthesize streaming when the
  service supports it; otherwise splits the text into sentences and synthesizes them one by one, so
  speech starts after the first sentence with any Wyoming TTS (e.g. Piper).

Capabilities are read from the service's `describe` → `info` answer.
