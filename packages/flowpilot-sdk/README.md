# flowpilot-sdk

Plugin SDK for FlowPilot (contracts v1, ADR-013): payload types (`AudioFormat`, `AudioChunk`,
`Transcript`, `Utterance`, `TextDelta`, `Response`, `Principal`, ...), the stream contracts
(`Envelope`, `InputStream`, `OutputStream`), port types and the node interface (`NodeSpec`,
`PortSpec`, `NodeContext`, `Node`), plus what nodes share through the runtime: the dialogue,
memory, quick commands, skills and voice profiles.
Message-based by design, so a plugin can run in-process or in a separate process.

This package is the source of truth for node signatures; writing a plugin: `CONTRIBUTING.md`.
