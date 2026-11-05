# Changelog

## 0.1.2

- Each deployed graph gets its own FlowPilot speech-to-text and agent entities, so every Assist
  assistant can run its own graph; the plain FlowPilot entities follow the main graph, which you
  star in the editor.
- Debugging nodes: Record audio (one WAV per turn on any audio wire, played back in live debug,
  kept out of backups) and Fixed text (sends a set text at once or word by word).
- Settings that name a Wyoming service, an entity, a model, a language, a voice or a speaker are
  picked from lists, with your own value one click away; nodes show names instead of raw values.
- Ctrl+S / Cmd+S deploys; two-finger scroll pans the canvas; "Refresh list" shows its progress.

## 0.1.1

- Adding the integration by hand on Home Assistant OS finds the FlowPilot app by itself instead of
  asking for an address and a token.
- New icons and logos for the app and the integration (light and dark), and the same mark in the
  editor.

## 0.1.0

- First release: the FlowPilot runtime with the graph editor in the Home Assistant sidebar
  (light and dark themes, English and Russian), live debug, drafts and deploy without restarts.
- Nodes: Home Assistant conversation agent, speech-to-text and text-to-speech; any Wyoming
  speech-to-text and text-to-speech service; voice command segmenter; first utterance.
- The FlowPilot integration finds the add-on by itself; repair issues when a graph refers to
  Home Assistant entities that are gone.
