# editor

Visual graph editor for FlowPilot (Vue 3 + Vue Flow). The runtime serves the build at `/editor/`.

- Pins and wires are colored by payload type; a round pin is a stream port, a diamond is a value port.
  Only ports of the same type connect, and an input takes one wire.
- Node settings forms come from each node's JSON Schema; `{"type": "secret"}` fields store a reference
  to the runtime secrets file, never the secret itself.
- Every change is autosaved as a draft (`<id>.draft.json`) and validated by the runtime; **Deploy**
  swaps the running graph without a restart (turns in flight finish on the old version).
- **Live debug** subscribes to the runtime trace stream: node status, reaction and delay, item counts
  on wires and where a turn failed, in three tabs: **Data** (what each node's inputs and outputs
  carried), **Journal** (what nodes logged: model rounds, tool calls, quick commands) and
  **Timeline** (each node's reaction and delay, the path to the answer's first sound). Traces carry
  no audio or text by themselves; a watched turn's port data is captured only while live debug is on
  (`flowpilot_server/captures.py`), audio as recordings the editor plays through the API.
- Pages besides the graph: **Secrets**, **Memory**, **Quick commands**, **Skills** and **Voices**
  (voice profiles for the Who speaks node).
- Light and dark themes, English and Russian.

## Develop

```sh
npm ci
npm run dev        # http://localhost:5173/editor/, API proxied to FLOWPILOT_URL (default :8765)
npm run build      # type check, then build into packages/flowpilot-server/src/flowpilot_server/editor/
```

The editor talks to the REST API in `flowpilot_server/editor_api.py` and the trace socket in
`flowpilot_server/tracehub.py`; it signs in with the runtime access token.
