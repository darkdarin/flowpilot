# Changelog

The app (**FlowPilot**: the runtime and the graph editor) and the integration (**FlowPilot
Assist**) are released together with one version, so Home Assistant offers to update both. Each
version says what changed in each of them: a part that says "No changes" does not need updating.

## 0.1.5

### App

- Gain and Automatic gain nodes for quiet microphones: a fixed gain, or speech brought to an even
  level (quiet speech raised, loud speech lowered, background noise left alone, no clipping). Put
  them in front of Voice command segmenter and speech-to-text.
- Live debug shows what each node reported about the selected turn right on the node: the speech
  level that came in, the gain applied, why a voice command ended.
- Number settings with a range get a slider with a field for exact values and a reset to the
  default. Voice command segmenter and Wyoming timeouts now have upper limits (for example, the
  longest command is at most 60 s).

### Integration

- No changes, no need to update.

## 0.1.4

- FlowPilot text-to-speech: pick `tts.flowpilot_<graph>` (or FlowPilot Text-to-speech for the
  main graph) in an Assist assistant and the answer is spoken by the graph's own text-to-speech
  nodes instead of a Home Assistant voice. Long answers stream; repeated answers come from Home
  Assistant's cache until you deploy a changed graph.
- A fallback voice in the FlowPilot integration options speaks what FlowPilot has no speech for:
  answers of local intents, satellite announcements, `tts.speak`, graphs without text-to-speech.
- Development nodes: the echo agent is gone (use Fixed answer with `{text}`); a beeping
  text-to-speech for trying the speech path without a voice service.

## 0.1.3

- Debug probe: one debugging node for audio and text. Put it next to any output and, in live
  debug, the selected turn's recording plays and its text shows right on the node. Debug probes
  run only while live debug is on (greyed out otherwise), so nothing is recorded unless you look.
- Fixed answer stands in for an agent: it answers with a set text, at once or word by word, and
  `{text}` repeats what was said.
- Try a graph without a satellite: in live debug the Client input node starts a turn with typed
  text, a microphone recording or an audio file.
- Graphs with the Record audio or Fixed text nodes of 0.1.2 are updated to the new nodes when
  loaded. Earlier recordings are no longer listed in the editor.
- Debug nodes are red; a node's run time is green unless it failed.

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
