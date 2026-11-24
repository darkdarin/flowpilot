# ha-integration

The integration itself lives in `custom_components/flowpilot/` at the repository root (the layout
HACS and the Marketplace install from); this directory is its test environment.

Home Assistant companion integration `custom_components/flowpilot` (**FlowPilot Assist**, installed
via the Marketplace or HACS).

It connects Home Assistant to a FlowPilot runtime (WebSocket, ADR-014) and provides, in Assist mode:

- a **speech-to-text** entity that hands the satellite audio to FlowPilot (external VAD off, raw
  audio) and returns the graph's final transcript; when exactly one satellite is listening, its
  entity, device and area go with the turn from the start;
- a **conversation** entity that returns the answer of the same turn, streaming it into the chat
  log so Home Assistant can start TTS early; called directly (chat, `conversation.process`) it
  starts a text turn;
- a **text-to-speech** entity that plays the speech the graph synthesized for the turn (ADR-017),
  with a fallback voice from the options for what the graph has no speech for;
- **host operations** the runtime calls: `ha.conversation.process` (a real Home Assistant agent for
  the graph's `ha.conversation` node), `ha.stt.process` and `ha.tts.synthesize` (any STT/TTS entity,
  audio streamed both ways), `ha.entities.list` (what the editor offers in its pickers),
  `ha.people.list`, `ha.turn.where` and `ha.names.list` (people, where a turn comes from, names
  Assist knows), `ha.llm.tools` and `ha.llm.call` (the Assist API's tools, the live context and
  `play_music` with Music Assistant, for the runtime's agents) and `ha.service.call` (the actions
  of skills);
- **repair issues** for deployed graphs that refer to entities or agents that are gone;
- **one speech-to-text, agent and text-to-speech entity per deployed graph**
  (`stt.flowpilot_<graph>`, `conversation.flowpilot_<graph>`, `tts.flowpilot_<graph>`), so each
  Assist assistant picks its graph; the plain "Speech-to-text", "Agent" and "Text-to-speech"
  follow the runtime's main graph. A voice turn runs the graph of its speech-to-text entity (it starts the turn); a
  different graph's agent in the same assistant only logs a warning;
- the option **Quiet media while a satellite listens** (`duck.py`): the playing media players of
  the satellite's area are turned down (or muted, paused) from the wake word until the command is
  heard.

The STT and conversation stages of one pipeline run are linked with a context variable (ADR-012);
this relies on Home Assistant internals and is verified per release with `scripts/e2e_ha.py`
(2026.10.4, 2026.11.0b4 and 2026.11.0 so far).

Supported Home Assistant versions: the current and the previous release (now 2026.11 and 2026.10).
CI runs the tests against the release pinned in `pyproject.toml`; the other one is checked with the
e2e script until the test harness for it is published.

Set up: with the FlowPilot add-on, Home Assistant discovers the runtime (Supervisor discovery) and
only asks to confirm; otherwise add the integration with the runtime's URL and token.

No external requirements; the protocol client is in `client.py`.

Checks (CI job `ha-integration`; the tests need `ffmpeg` installed), from this directory:

```sh
uv sync && uv run mypy && uv run pytest
```
