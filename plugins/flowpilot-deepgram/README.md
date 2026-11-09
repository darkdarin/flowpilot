# flowpilot-deepgram

[Deepgram](https://deepgram.com) nodes for [FlowPilot](https://github.com/darkdarin/flowpilot), talking
to the Deepgram Flux WebSocket APIs directly:

- `deepgram.stt` — streaming speech-to-text with Flux (`/v2/listen`): partial results, Flux's own
  end-of-turn detection, key terms and wake word removal. `flux-general-multi` speaks English,
  Spanish, French, German, Hindi, Russian, Portuguese, Japanese, Italian and Dutch;
- `deepgram.tts` — streaming text-to-speech with Flux TTS (`/v2/speak`): speaks while the agent is
  still writing, in any of the Flux voices (`flux-<voice>-<language>`, 40+ languages).

Both need a Deepgram API key, kept as a runtime secret (`{"secret": "deepgram"}`).
