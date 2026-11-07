# Changelog

The app (**FlowPilot**: the runtime and the graph editor) and the integration (**FlowPilot
Assist**) are released together with one version, so Home Assistant offers to update both. Each
version says what changed in each of them: a part that says "No changes" does not need updating.

## 0.1.20

### App

- **Changing a quick command no longer makes a copy.** Asked to add phrases to a quick command,
  the LLM agent saved a second one and the first stayed. It now changes the command itself: add
  or remove phrases, replace what it does or its answer; saving a command with a phrase another
  one has replaces that one. Remove copies made before on the Quick commands page.

### Integration

- No changes, no need to update.

## 0.1.19

### App

- **The LLM agent teaches quick commands.** After it did something with Home Assistant that
  people will ask for again (several devices at once, such as "movie night", or a device called
  by a nickname), the agent offers to remember it and, when you agree, saves the phrases and
  exactly what it did. Quick commands then run it at once, without an LLM, as the person who
  speaks, and match the phrase in any form of its words. Ask "remember it as a quick command" or
  "forget the quick command…" yourself too. Only people Home Assistant knows can teach. The new
  setting **Teaches quick commands** (under Tools, on by default) turns it off.
- **Quick commands page** in the editor (the lightning button): every learned command with its
  phrases, what it does step by step, its answer, who taught it and how often it was used; edit
  the phrases and the answer, or remove it.

### Integration

- Tells the app whether the devices a Home Assistant tool touched are in the speaker's room, so
  learned commands can be confirmed with a sound too. Update it together with the app.

## 0.1.18

### App

- **Quick commands understand names in any case.** Home Assistant compares names literally, so
  "включи свет в гостиной" missed the area "Гостиная". When it does not understand a phrase, the
  second try now writes every room, floor and device name the way Home Assistant has it ("в
  гостиной" → "Гостиная", "настольную лампу" → "Настольная лампа"), using a Russian dictionary.
- **A sound instead of words**, as Home Assistant does: Quick commands get the output ack and the
  setting Confirm with a sound (in the same room as the speaker, always, never). Connect the new
  **Sound signal** node to it: it plays Home Assistant's confirmation sound, or a short sound of
  your own uploaded in its settings. Join its sound with text-to-speech using First present, which
  now joins audio too. Without ack connected, answers are spoken as before.

### Integration

- Lists the names Assist knows and tells whether a command touched only devices in the
  speaker's room. Update it together with the app.

## 0.1.17

### App

- **Home Assistant quick commands.** A new node runs the commands Home Assistant understands by
  itself (turn on the light, set the temperature, start a timer, your own custom sentences) in a
  fraction of a second, without an LLM; everything else goes on unchanged to the next agent.
  When Home Assistant does not understand a phrase, it tries once more without punctuation,
  politeness and fillers ("please", "listen", "can you"…) and the words you list, such as the
  assistant's name. What a quick command did becomes part of the dialogue, so the LLM agent knows
  it ("turn it back on").
- **First present** (formerly First utterance) joins two branches of any kind of text: its output
  has the type connected to its inputs, and a text stream passes through as it comes. Use it to
  join a quick command and an agent in front of the client output and text-to-speech.
- **One text input** on Debug probe and Text converter: connect any kind of text. Graphs are
  updated when the app starts: a probe that watched several texts becomes one probe per text.
- Ports may take several types: wires, pins and the legend show the type each port carries in
  the graph, and the editor only lets you connect what fits.
- The dialogue is shared by the nodes of a session; the LLM agent keeps its dialogue there now.

### Integration

- Tells the app why Home Assistant's own agent did not handle a phrase, so quick commands pass
  only those on. Update it together with the app.

## 0.1.16

### App

- **The LLM agent's journal in live debug.** Select the agent (on the canvas or in the waterfall)
  and a journal opens next to the waterfall: the facts and the dialogue it used, the whole prompt
  sent to the model, every model request and answer, and every tool call with its arguments,
  result and time (remembering, controlling the home, playing music). Failed calls stand out.
  The journal is written only while live debug is on; otherwise what people said stays out of the
  trace, as before.

### Integration

- No changes, no need to update.

## 0.1.15

### App

- No changes, no need to update.

### Integration

- **The LLM agent plays music with Music Assistant.** Home Assistant's own tools for language
  models can pause or skip, but not find and start music; FlowPilot Assist adds a tool that does.
  Ask for a song, an artist, an album, a playlist or a radio station, even misspelled: Music
  Assistant searches every source and your library's playlists, and the best match plays at once
  (with several close matches the agent asks which). Without a place, it plays on the Music
  Assistant player in the room of the speaker that heard you; "play some music" plays the
  library's endless mix, or the music the agent remembers you like. Only players exposed to Assist
  are used. Restart Home Assistant after updating.

## 0.1.14

### App

- **The LLM agent controls the home.** It gets Home Assistant's own tools for language models
  (the same that Home Assistant's OpenAI or Ollama conversation agents get): turning devices on
  and off, brightness, climate, media, the current state of the home, timers on satellites. Only
  entities exposed to Assist (Settings → Voice assistants → Expose) can be used, and Home Assistant
  runs every action as the person who speaks; on a satellite, "here" means its area. The new
  setting **Controls the home** (under Tools, on by default) turns it off. Needs a model with
  function calling and the integration of this version.

### Integration

- Gives the app Home Assistant's tools for language models and runs them for the LLM agent.
  Update it together with the app, then restart Home Assistant.

## 0.1.13

### App

- **Live debug shows where time goes in a streaming turn.** Every node used to show about the same
  time, because with streamed audio all of them run for the whole turn. Now each node shows its
  **reaction** (first input to first output: how quickly it starts answering) and the time **after
  its input** (end of input to end of output: what it still did once it had everything). The
  waterfall draws when each node's output flowed and where its input ended, the canvas shows the
  reaction on the node, and the turn's headline shows the time from the end of speech to the first
  text and the first sound.

### Integration

- No changes, no need to update.

## 0.1.12

### App

- **The LLM agent knows who it talks to and who is home.** On every turn it gets the home's people
  from Home Assistant (people with where they are, and users without a person) and is told which
  of them is talking: the Home Assistant user of the Assist chat, or the Telegram user mapped in
  Telegram Assist. The new setting **Knows the household** (under Memory, on by default) turns it
  off. Needs the integration of this version.

### Integration

- Gives the app the home's people and where they are, so the LLM agent knows who it talks to.
  Update it together with the app.

## 0.1.11

### App

- **Reasoning setting for the LLM agent**: how much a reasoning model thinks before answering
  (none, low, medium, high). Thinking helps with tricky questions but delays the first word; None
  suits voice. Not set, the model decides and nothing is sent, so APIs that do not know the setting
  keep working.
- **Remembering works with OpenAI's reasoning models** (such as gpt-6-sol). They refuse tools while
  reasoning, and the agent used to answer without them, so it could not remember anything. Now it
  turns reasoning off for itself and keeps the tools; live debug shows `llm.agent.no_reasoning`.
  Set **Reasoning** to None to skip that first refused request.

### Integration

- No changes, no need to update.

## 0.1.10

### App

- **The agent can remember on its own.** The LLM agent's new setting **Remembers facts**: when
  asked (as before), on its own too, or never. On its own, it also keeps what it learns about the
  home and the family (names, birthdays, pets, routines, habits), never health, money, passwords or
  private matters, and says so in a few words. What it keeps is on the Memory page, marked as
  written by the assistant. **Remember on request** turned off becomes **Never**.
- **Memory settings in one place.** The agent's dialogue, memory and remembering settings open
  from one entry, **Memory**, in the node's settings.
- The agent no longer answers that it cannot remember things for later: it is told it has a
  long-term memory. Facts about the family, pets and the house are kept as the home's; when it is
  not known who is speaking, facts are kept as the home's instead of being refused. The same fact
  is not kept twice.

### Integration

- No changes, no need to update.

## 0.1.9

### App

- **Memory.** A new **Memory** page in the editor (the button next to Secrets) keeps facts the
  assistant knows: the home's, for everyone, and each person's own, only for them. LLM agents put
  them into their instructions on every turn; edits apply at once.
- **The agent remembers on request.** Say "remember that…" or "forget…", and the LLM agent keeps
  or removes the fact itself (needs a model with function calling; the new setting **Remember on
  request** turns it off). A personal fact needs to know who is speaking: the Home Assistant user of
  the Assist chat, or a Telegram user mapped in Telegram Assist; others' personal facts are never
  shown or forgotten.
- **Dialogues survive restarts and graph edits.** They used to be forgotten on every Deploy and app
  restart. A Telegram chat stays one dialogue until `/new`; the new agent setting **Dialogue kept,
  hours** (24) says when a quiet dialogue is forgotten.
- **Voice or text turn has two outputs**: one for spoken turns, one for typed ones, instead of a
  setting. Graphs using it are updated when the app starts.

### Integration

- Lists Home Assistant users to the app, so facts on the Memory page can belong to a person.
  Update it together with the app.

## 0.1.8

### App

- **LLM agent.** A lightweight agent for any OpenAI-compatible API (OpenAI, OpenRouter, Ollama,
  LM Studio…): the answer is streamed, so speech starts on the first words; the dialogue is
  remembered per conversation; instructions can use the current date, time, weekday and language;
  the model is picked from the list the API offers.
- **Voice or text turn.** A filter that lets a text stream through only in spoken turns (or only in
  typed ones). Put it in front of text-to-speech, and typed turns (Assist chat, automations,
  Telegram through Assist) get their answer as text without synthesizing speech.
- Long text settings (an agent's instructions) are edited in a multi-line field.

### Integration

- No changes, no need to update.

## 0.1.7

### App

- **Cloned voices in Soniox text-to-speech.** The voice list adds the voices you cloned in Soniox,
  by name, with a note when a voice is not yet ready for the chosen model.
- **Text converter.** One node turns recognized speech, an utterance, a text stream or an agent's
  answer into all four: for example an answer that comes whole into a stream for text-to-speech,
  or one agent's answer into the next agent's utterance.
- **Client input and output are always there.** Every graph has them exactly once: they are no
  longer in the palette, cannot be deleted, and a graph without one gets it back when opened.
- Node subtitles show the names of choices a node lists itself (a voice, a model) instead of raw
  ids.

### Integration

- No changes, no need to update.

## 0.1.6

### App

- **Soniox nodes.** Soniox speech-to-text and text-to-speech talk to Soniox directly, with every
  setting in the node: model, language, terms and context, how quickly the end of speech is
  detected, the longest command, wake words to cut; voice, speed and sample rate for speech. Speech
  recognition streams partial results and detects the end of speech itself, so it needs no voice
  command segmenter; text-to-speech speaks while the agent is still writing. The Soniox Wyoming app
  is no longer needed for FlowPilot (Assist without FlowPilot still uses it).
- **Secrets.** API keys are entered once under Secrets in the editor (the key button in the top
  bar) and picked by name in node settings. A value is never shown again; graphs that use a secret
  restart when it changes, and a graph whose secret is not set shows a warning.

### Integration

- Renamed to **FlowPilot Assist**, so its updates are not confused with the FlowPilot app's.
  Settings and entities stay as they are.

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
