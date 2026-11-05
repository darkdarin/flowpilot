# FlowPilot

A visual orchestrator for voice and AI flows, built for Home Assistant users, with a core that does not
depend on Home Assistant.

Instead of the fixed `wake word → STT → conversation → TTS` chain, FlowPilot runs a graph of typed nodes:
parallel branches (for example streaming STT and speaker identification on the same audio), joins,
policy routing, agents and tools. Providers and devices are plugins.

> **Status:** early design and scaffolding. Nothing here is usable yet.

## How it connects to Home Assistant

FlowPilot owns the whole turn and runs the graph itself. Audio reaches it in one of two ways:

- **Inside Assist (mode A, default).** Works with any `assist_satellite` (ESPHome, Wyoming, VoIP,
  third-party integrations). A companion integration provides thin speech-to-text and conversation
  entities that hand the audio to FlowPilot and return its result.
- **Direct input (mode B).** A device sends audio straight to FlowPilot, bypassing Assist.

## Run with Docker

```sh
docker build -t flowpilot .
docker run -d --name flowpilot -p 8765:8765 -v flowpilot-data:/data flowpilot
docker exec flowpilot cat /data/token     # access token for the editor and the HA integration
```

The first start creates `/data/config.json`, `/data/token` and a starter graph. The editor is at
`http://<host>:8765/editor/`; add the FlowPilot integration in Home Assistant with the same URL and
token.

## Home Assistant add-on

The same image runs as an add-on (`addon/`): the editor opens from the Home Assistant sidebar
(Ingress, no token) and the integration finds the runtime through Supervisor discovery. Until the
add-on is published, install it as a local add-on:

```sh
python scripts/build_addon.py     # -> dist/addon/flowpilot/
```

Copy `dist/addon/flowpilot` to `/addons/flowpilot` on the Home Assistant machine (Samba or SSH
add-on), then Settings → Add-ons → Add-on Store → ⋮ → Check for updates; it appears under Local
add-ons and Home Assistant builds it there. Requires Home Assistant 2026.10 or newer.

## Repository layout

| Path | What |
|---|---|
| `packages/flowpilot-sdk` | Plugin SDK: message contracts and node interface |
| `packages/flowpilot-core` | Runtime: graph validation and execution |
| `plugins/` | Node plugins (STT, speaker ID, LLM, TTS, ...) |
| `ha-integration/` | Home Assistant companion integration |
| `editor/` | Visual graph editor (Vue 3 + Vue Flow) |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
