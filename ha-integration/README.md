# ha-integration

Home Assistant companion integration `custom_components/flowpilot` (to be distributed via HACS).

It connects Home Assistant to a FlowPilot runtime (WebSocket, ADR-014) and provides, in Assist mode:

- a **speech-to-text** entity that hands the satellite audio to FlowPilot (external VAD off, raw
  audio) and returns the graph's final transcript;
- a **conversation** entity that returns the answer of the same turn, streaming it into the chat
  log so Home Assistant can start TTS early; called directly (chat, `conversation.process`) it
  starts a text turn;
- **host operations** the runtime calls: `ha.conversation.process` (a real Home Assistant agent for
  the graph's `ha.conversation` node), `ha.stt.process` and `ha.tts.synthesize` (any STT/TTS entity,
  audio streamed both ways) and `ha.entities.list` (what the editor offers in its pickers);
- **repair issues** for deployed graphs that refer to entities or agents that are gone.

The STT and conversation stages of one pipeline run are linked with a context variable (ADR-012);
this relies on Home Assistant internals and is verified per release with `scripts/e2e_ha.py`
(2026.10.4, 2026.11.0b4 and 2026.11.0 so far).

Supported Home Assistant versions: the current and the previous release (now 2026.11 and 2026.10).
CI runs the tests against the release pinned in `pyproject.toml`; the other one is checked with the
e2e script until the test harness for it is published.

Set up: with the FlowPilot add-on, Home Assistant discovers the runtime (Supervisor discovery) and
only asks to confirm; otherwise add the integration with the runtime's URL and token.

No external requirements; the protocol client is in `client.py`.
