"""Server nodes pass the plugin conformance kit."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node
from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    Node,
    Principal,
    Response,
    TextDelta,
    Transcript,
    TurnContext,
    Utterance,
)
from flowpilot_server.nodes import ClientInput, ClientSink, ClientSource, HaConversation

FMT = AudioFormat(sample_rate=16000)


class RecordingOutlet:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def transcript(self, text: str, final: bool) -> None:
        self.events.append("transcript")

    async def delta(self, text: str) -> None:
        self.events.append("delta")

    async def response(self, response: Response) -> None:
        self.events.append("response")

    async def speech_start(self, fmt: AudioFormat) -> None:
        self.events.append("speech_start")

    async def speech_chunk(self, pcm: bytes) -> None:
        self.events.append("speech_chunk")

    async def speech_end(self) -> None:
        self.events.append("speech_end")


class FakeHost:
    def offers(self, op: str) -> bool:
        return op == "ha.conversation.process"

    async def call(self, turn_id: str, op: str, args: Mapping[str, Any]) -> Any:
        return {"speech": f"echo: {args['text']}"}


def prepare_source(node: Node, ctx: TurnContext) -> None:
    assert isinstance(node, ClientSource)
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    for _ in range(3):
        queue.put_nowait(bytes(640))
    queue.put_nowait(None)
    node.inputs[ctx.turn_id] = ClientInput(text="hi", audio_format=FMT, audio=queue)


def prepare_sink(node: Node, ctx: TurnContext) -> None:
    assert isinstance(node, ClientSink)
    node.outlets[ctx.turn_id] = RecordingOutlet()


async def test_client_source_conforms() -> None:
    assert_conforms(await check_node(ClientSource, prepare=prepare_source))


async def test_client_sink_conforms() -> None:
    sample = {
        "transcript": InputFeed([Transcript("hi", final=True)]),
        "delta": InputFeed([TextDelta("hi")]),
        "response": InputFeed([Response("hi")]),
        "speech": InputFeed([AudioChunk(bytes(640), 0)], meta=FMT.as_meta()),
    }
    assert_conforms(await check_node(ClientSink, sample=sample, prepare=prepare_sink))


async def test_ha_conversation_conforms() -> None:
    sample = {
        "utterance": InputFeed([Utterance("turn on the light", "en")]),
        "principal": InputFeed([Principal.unknown()]),
    }
    report = await check_node(
        HaConversation, config={"agent_id": "conversation.ha"}, sample=sample, host=FakeHost()
    )
    assert_conforms(report)


async def test_ha_conversation_without_host_fails_closed() -> None:
    sample = {"utterance": InputFeed([Utterance("hi")])}
    report = await check_node(HaConversation, sample=sample)
    # The sample run raises HostUnavailable (fail closed): that is a C5 violation by design,
    # which tells the plugin author the node needs a host in tests.
    assert any(v.startswith("C5") and "HostUnavailable" in v for v in report.violations)
