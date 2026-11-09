"""Server nodes pass the plugin conformance kit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node
from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    HostAudio,
    Node,
    Principal,
    Response,
    TextDelta,
    Transcript,
    TurnContext,
    Utterance,
)
from flowpilot_server.nodes import (
    ClientInput,
    ClientSink,
    ClientSource,
    HaConversation,
    HaStt,
    HaTts,
)

FMT = AudioFormat(sample_rate=16000)


class RecordingOutlet:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def transcript(self, text: str, final: bool) -> None:
        self.events.append("transcript")

    async def delta(self, text: str) -> None:
        self.events.append("delta")

    async def response(self, response: Response, acknowledge: str | None = None) -> None:
        self.events.append("response")

    async def speech_start(self, fmt: AudioFormat) -> None:
        self.events.append("speech_start")

    async def speech_chunk(self, pcm: bytes) -> None:
        self.events.append("speech_chunk")

    async def speech_end(self) -> None:
        self.events.append("speech_end")


class FakeHost:
    def offers(self, op: str) -> bool:
        return op in {"ha.conversation.process", "ha.stt.process", "ha.tts.synthesize"}

    async def call(
        self,
        turn_id: str,
        op: str,
        args: Mapping[str, Any],
        *,
        audio_in: AsyncIterable[bytes] | None = None,
        audio_format: AudioFormat | None = None,
    ) -> Any:
        if audio_in is not None:
            return {"text": f"{sum([len(c) async for c in audio_in])} bytes"}
        return {"speech": f"echo: {args['text']}"}

    @asynccontextmanager
    async def stream(
        self, turn_id: str, op: str, args: Mapping[str, Any]
    ) -> AsyncIterator[HostAudio]:
        yield FakeAudio(str(args["text"]).encode())


class FakeAudio:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.format: AudioFormat | None = AudioFormat(sample_rate=16000)

    async def _iter(self) -> AsyncIterator[bytes]:
        yield self.pcm

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def result(self) -> Any:
        return {}


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


async def test_ha_stt_conforms() -> None:
    audio = InputFeed([AudioChunk(bytes(640), 0)] * 3, meta=FMT.as_meta())
    report = await check_node(
        HaStt, config={"entity_id": "stt.cloud"}, sample={"audio": audio}, host=FakeHost()
    )
    assert_conforms(report)


async def test_ha_tts_conforms() -> None:
    text = InputFeed([TextDelta("Hello there, my friend. "), TextDelta("Bye.")])
    report = await check_node(
        HaTts, config={"engine": "tts.cloud"}, sample={"text": text}, host=FakeHost()
    )
    assert_conforms(report)


async def test_client_sink_tells_the_client_to_confirm_with_a_sound() -> None:
    """What comes on ack makes the answer a sound, with what was done when the data says."""
    from flowpilot_core.conformance import InputFeed, run_node_once  # noqa: PLC0415
    from flowpilot_core.runtime import RuntimeNodeContext  # noqa: PLC0415

    class Outlet(RecordingOutlet):
        acknowledge: str | None = "unset"

        async def response(self, response: Response, acknowledge: str | None = None) -> None:
            self.acknowledge = acknowledge

    async def sent(ack: InputFeed | None) -> str | None:
        node = ClientSink()
        await node.start(RuntimeNodeContext("out", {}, {}, frozenset()))
        outlet = Outlet()

        def prepare(node: Node, ctx: TurnContext) -> None:
            assert isinstance(node, ClientSink)
            node.outlets[ctx.turn_id] = outlet

        inputs = {"response": InputFeed(items=[Response("Выключила свет")])}
        if ack is not None:
            inputs["ack"] = ack
        run = await run_node_once(node, inputs, prepare=prepare)
        assert run.error is None, run.error
        return outlet.acknowledge

    off = Response("Выключила свет", data={"acknowledge": "off"})
    assert await sent(InputFeed(items=[off])) == "off"
    assert await sent(InputFeed(items=[Response("ок")])) == "done"  # says nothing of what
    assert await sent(InputFeed(end="absent")) is None  # wired, nothing came: speak
    assert await sent(None) is None
