# flowpilot-llm

A lightweight LLM agent node (`llm.agent`) for [FlowPilot](https://github.com/darkdarin/flowpilot):
any OpenAI-compatible chat API (OpenAI, OpenRouter, Ollama, LM Studio, …), the answer streamed
straight into text-to-speech, and the dialogue's history kept per session.

With a model that calls functions, the agent controls the home with Home Assistant's Assist tools
(and plays music through Music Assistant), remembers facts about the home and its people, runs the
owner's skills, teaches quick commands and may confirm a done command with the satellite's sound.
It may also look up fresh facts on the web (`web_search`): Tavily, Brave Search or a self-hosted
SearXNG, set in the node's settings. Speech markup (Silero) has the model mark the intonation of
questions and the emphasised word for Silero voices; the marks go only to text-to-speech.

`llm.voice` (Voice agent) hears and answers with one speech-to-speech model over the OpenAI
Realtime API (or a compatible one, such as Yandex AI Studio's), with the same memory and tools.
