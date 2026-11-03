# editor

Visual graph editor for FlowPilot (Vue 3 + Vue Flow). The runtime serves the build at `/editor/`.

- Pins and wires are colored by payload type; a round pin is a stream port, a diamond is a value port.
  Only ports of the same type connect, and an input takes one wire.
- Node settings forms come from each node's JSON Schema; `{"type": "secret"}` fields store a reference
  to the runtime secrets file, never the secret itself.
- Every change is autosaved as a draft (`<id>.draft.json`) and validated by the runtime; **Deploy**
  swaps the running graph without a restart (turns in flight finish on the old version).
- **Live debug** subscribes to the runtime trace stream: node status and timings, item counts on wires,
  host calls, and a waterfall per turn. Traces never carry audio or text.
- Light and dark themes, English and Russian.

## Develop

```sh
npm ci
npm run dev        # http://localhost:5173/editor/, API proxied to FLOWPILOT_URL (default :8765)
npm run build      # type check, then build into packages/flowpilot-server/src/flowpilot_server/editor/
```

The editor talks to the REST API in `flowpilot_server/editor_api.py` and the trace socket in
`flowpilot_server/tracehub.py`; it signs in with the runtime access token.
