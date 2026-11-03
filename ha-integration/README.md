# ha-integration

Home Assistant companion integration `custom_components/flowpilot` (to be distributed via HACS).

It connects Home Assistant to a FlowPilot runtime (WebSocket, ADR-014) and provides, in Assist mode:

- a **speech-to-text** entity that hands the satellite audio to FlowPilot (external VAD off, raw
  audio) and returns the graph's final transcript;
- a **conversation** entity that returns the answer of the same turn, streaming it into the chat
  log so Home Assistant can start TTS early; called directly (chat, `conversation.process`) it
  starts a text turn;
- the **host operation** `ha.conversation.process`, which runs a real Home Assistant agent for the
  graph's `ha.conversation` node.

The STT and conversation stages of one pipeline run are linked with a context variable (ADR-012);
this relies on Home Assistant internals and is verified per release with `scripts/e2e_ha.py`
(2026.10.4 and 2026.11.0b4 so far).

No external requirements; the protocol client is in `client.py`.
