# flowpilot-server

The FlowPilot runtime as a network service. Clients (the Home Assistant companion integration, later a
browser or other channels) connect over WebSocket, start turns, stream audio and receive transcripts,
response text and speech. Over the same connection the server asks the client to perform host
operations, such as running a Home Assistant conversation agent (ADR-014).

The protocol is described in `src/flowpilot_server/protocol.py`.
