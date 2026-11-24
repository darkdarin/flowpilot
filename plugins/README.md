# plugins

Node plugins for FlowPilot. Each plugin is a separate package built on `flowpilot-sdk` and
registers its nodes under the `flowpilot.nodes` entry point group:

- `flowpilot-audio`: gain and automatic gain;
- `flowpilot-vad`: voice command segmenter;
- `flowpilot-wyoming`: speech-to-text and text-to-speech with any Wyoming service;
- `flowpilot-soniox`, `flowpilot-deepgram`: cloud speech-to-text and text-to-speech;
- `flowpilot-llm`: LLM agent and voice agent;
- `flowpilot-speech`: text for speech for Russian voices.
