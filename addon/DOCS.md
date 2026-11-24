# FlowPilot

## Install

1. Install and start the app. The **FlowPilot** item appears in the sidebar: that is the graph
   editor, already signed in through Home Assistant.
2. Install the **FlowPilot Assist** integration from the same repository: Settings → Marketplace → Add from
   link → `darkdarin/flowpilot` (on Home Assistant 2026.10: HACS → Custom repositories, type
   Integration), then restart Home Assistant.
3. Home Assistant finds the app's runtime by itself: confirm the discovered **FlowPilot Assist** in
   Settings → Devices & services. You do not need a URL or a token.
4. In Settings → Voice assistants, create (or edit) an assistant with the same FlowPilot graph as
   its speech-to-text and conversation agent (each deployed graph has its own pair; **FlowPilot
   Speech-to-text** and **FlowPilot Agent** follow the main graph), and optionally its
   text-to-speech to have the graph speak. Satellites using that assistant now run your graph.

The add-on starts with a graph `main` that answers with the Home Assistant agent. Open it in the
editor to add speech-to-text, other agents or text-to-speech, then press **Deploy**.

## Options

- **Log level**: how much the runtime writes to the add-on log.
- **Development nodes**: nodes for testing without speech services.

## Data

Everything lives in the add-on's data folder and is part of its backups: `config.json` (ports,
default graph), `token` (for clients outside Home Assistant), `graphs/` (each graph and its draft),
`secrets.json` (API keys entered under Secrets in the editor), `memory.json`,
`quick_commands.json` and `skills.json` (the agent's memory, quick commands and skills),
`voices.json` (voice profiles for Who speaks) and `sessions.json` (dialogues). Live debug
recordings and downloaded voice models (`models/`) are kept out of backups.

## Clients outside Home Assistant

Map port 8765 in the add-on's network settings to reach the runtime API from other machines (the
browser dev client at `/`, `POST /api/turn`, the WebSocket protocol). They need the token from the
`token` file.
