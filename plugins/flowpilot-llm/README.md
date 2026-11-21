# flowpilot-llm

A lightweight LLM agent node for [FlowPilot](https://github.com/darkdarin/flowpilot): any
OpenAI-compatible chat API (OpenAI, OpenRouter, Ollama, LM Studio, …), the answer streamed
straight into text-to-speech, and the dialogue's history kept per session.

With a model that calls functions, the agent may also look up fresh facts on the web
(`web_search`): Tavily, Brave Search or a self-hosted SearXNG, set in the node's settings.

`llm.voice` (Voice agent) hears and answers with one speech-to-speech model over the OpenAI
Realtime API (or a compatible one, such as Yandex AI Studio's), with the same memory and tools.
