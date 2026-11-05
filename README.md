# FlowPilot

A visual orchestrator for voice and AI flows, built for Home Assistant users, with a core that does not
depend on Home Assistant.

Instead of the fixed `wake word → STT → conversation → TTS` chain, FlowPilot runs a graph of typed nodes:
parallel branches (for example streaming STT and speaker identification on the same audio), joins,
policy routing, agents and tools. Providers and devices are plugins.

> **Status:** early (0.1). Works inside Assist with Home Assistant's STT, TTS and conversation
> agents and with any Wyoming speech service; speaker identification, LLM agents and tools are next.

## How it connects to Home Assistant

FlowPilot owns the whole turn and runs the graph itself. Audio reaches it in one of two ways:

- **Inside Assist (mode A, default).** Works with any `assist_satellite` (ESPHome, Wyoming, VoIP,
  third-party integrations). A companion integration provides thin speech-to-text and conversation
  entities that hand the audio to FlowPilot and return its result.
- **Direct input (mode B).** A device sends audio straight to FlowPilot, bypassing Assist.

## Install in Home Assistant

Requires Home Assistant OS or Supervised, 2026.10 or newer. Two parts, both from this repository:
the **app** (the FlowPilot runtime with the graph editor) and the **integration** (connects Assist
to it).

### 1. The app

[![Open the app in Home Assistant](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=988b5cb9_flowpilot&repository_url=https%3A%2F%2Fgithub.com%2Fdarkdarin%2Fflowpilot)

Or: **Settings → Apps → App Store → ⋮ → Repositories**, add `https://github.com/darkdarin/flowpilot`,
then install **FlowPilot** and start it. The **FlowPilot** sidebar item opens the graph editor.

### 2. The integration

[![Open in the Marketplace](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=darkdarin&repository=flowpilot&category=integration)

On 2026.11 and newer: **Settings → Marketplace → Add from link**, enter `darkdarin/flowpilot`
(type **Integration** if asked), install **FlowPilot** and restart Home Assistant. On 2026.10 do the
same in HACS (**⋮ → Custom repositories**, type **Integration**).

### 3. Connect

**Settings → Devices & services** shows the discovered **FlowPilot**: confirm it, no URL or token
needed. Then in **Settings → Voice assistants** pick **FlowPilot** as the speech-to-text engine and
the conversation agent of an assistant; satellites using it now run your graph.

## Run with Docker (without the app)

```sh
docker run -d --name flowpilot -p 8765:8765 -v flowpilot-data:/data ghcr.io/darkdarin/flowpilot
docker exec flowpilot cat /data/token     # access token for the editor and the HA integration
```

The first start creates `/data/config.json`, `/data/token` and a starter graph. The editor is at
`http://<host>:8765/editor/`; add the FlowPilot integration with the same URL and token. Build the
image yourself with `docker build -t flowpilot .`.

For development, `python scripts/build_addon.py` assembles a local app (`dist/addon/flowpilot`, copy
it to `/addons` on the Home Assistant machine) that Home Assistant builds from the current sources.

## Repository layout

| Path | What |
|---|---|
| `packages/flowpilot-sdk` | Plugin SDK: message contracts and node interface |
| `packages/flowpilot-core` | Runtime: graph validation and execution |
| `plugins/` | Node plugins (STT, speaker ID, LLM, TTS, ...) |
| `custom_components/flowpilot` | Home Assistant companion integration (installed via Marketplace/HACS) |
| `ha-integration/` | Test environment for the integration (pinned Home Assistant) |
| `addon/` | Home Assistant app (add-on) |
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
