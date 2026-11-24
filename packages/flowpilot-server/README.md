# flowpilot-server

The FlowPilot runtime as a network service. Clients (the Home Assistant companion integration, the
browser test client at `/`, `POST /api/turn`) connect over WebSocket or HTTP, start turns, stream
audio and receive transcripts, response text and speech. Over the same connection the server asks
the client to perform host operations, such as running a Home Assistant conversation agent
(ADR-014). It also serves the graph editor at `/editor/` with its REST API (`editor_api.py`) and
live debug trace socket (`tracehub.py`), and loads node plugins from the `flowpilot.nodes` entry
point group.

Nodes that live here: Client input and output (`client.source`, `client.sink`), Home Assistant
speech-to-text, text-to-speech, conversation agent and quick commands (`ha.stt`, `ha.tts`,
`ha.conversation`, `ha.quick`), Condition (`logic.condition`), Who speaks (`identity.speaker`),
Fixed answer (`debug.answer`) and, with the `dev_nodes` option, `dev.fixed_stt` and `dev.beep_tts`.

The protocol is described in `src/flowpilot_server/protocol.py`.
