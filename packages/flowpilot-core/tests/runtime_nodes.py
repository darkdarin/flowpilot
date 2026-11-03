"""Behavioural fake nodes for the example graph (same specs as conftest.example_specs).

Audio chunks carry a speaker tag in their first bytes, so tests can check that transcript, speaker
and response of concurrent turns never mix.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from flowpilot_core import NodeRegistry
from flowpilot_sdk import (
    Assurance,
    AudioChunk,
    AudioFormat,
    Evidence,
    Node,
    NodeContext,
    NodeSpec,
    Principal,
    Response,
    SpeakerMatch,
    SpeakerStatus,
    StreamError,
    Transcript,
    TurnSession,
    Utterance,
)

from .conftest import example_specs

SPECS = {s.type: s for s in example_specs()}
FMT = AudioFormat(sample_rate=16000)
CHUNK_SAMPLES = 320  # 20 ms


def tagged_audio(tag: str, chunks: int = 25) -> list[bytes]:
    size = CHUNK_SAMPLES * FMT.bytes_per_sample
    first = tag.encode().ljust(size, b"\x00")
    return [first] + [bytes(size)] * (chunks - 1)


def tag_of(pcm: bytes) -> str:
    return pcm.rstrip(b"\x00").decode()


class Base:
    spec: ClassVar[NodeSpec]
    config: dict[str, Any]

    async def start(self, ctx: NodeContext) -> None:
        self.config = dict(ctx.config)

    async def stop(self) -> None:
        return None


@dataclass
class Feed:
    audio: list[bytes] | None = None
    text: str | None = None
    chunk_delay_s: float = 0.0


class Source(Base):
    spec = SPECS["ha.assist.source"]

    def __init__(self) -> None:
        self.feeds: dict[str, Feed] = {}

    async def run_turn(self, turn: TurnSession) -> None:
        feed = self.feeds.pop(turn.context.turn_id)
        if feed.text is not None:
            await turn.output("utterance").send(Utterance(feed.text, "en"))
        if feed.audio is not None:
            audio = turn.output("audio")
            await audio.open(FMT.as_meta())
            for i, pcm in enumerate(feed.audio):
                await audio.send(AudioChunk(pcm, i * CHUNK_SAMPLES))
                await asyncio.sleep(feed.chunk_delay_s)


class Stt(Base):
    spec = SPECS["wyoming.stt"]

    async def run_turn(self, turn: TurnSession) -> None:
        audio = turn.input("audio")
        if await audio.header() is None:
            return  # text turn: transcript and utterance stay absent
        tag, n = "", 0
        async for chunk in audio:
            if n == 0:
                tag = tag_of(chunk.pcm)
            n += 1
            if n % 10 == 0:
                await turn.output("transcript").send(Transcript(f"heard {tag}…", final=False))
        if self.config["uri"] == "bad-type":
            await turn.output("utterance").send(Transcript("oops", final=True))
        text = f"heard {tag}"
        await turn.output("transcript").send(Transcript(text, final=True, language="en"))
        await turn.output("utterance").send(Utterance(text, "en"))


class Speaker(Base):
    spec = replace(SPECS["speaker.onnx"], turn_timeout_s=0.5)

    async def run_turn(self, turn: TurnSession) -> None:
        mode = self.config.get("mode", "ok")
        audio = turn.input("audio")
        if await audio.header() is None:
            await turn.output("speaker").send(SpeakerMatch.unknown("no_audio"))
            return
        if mode == "fail":
            raise RuntimeError("model crashed")
        if mode == "hang":
            await asyncio.Event().wait()
        async for chunk in audio:
            audio.close()  # one chunk is enough for the fake model
            turn.trace("speaker.window", chunks=1)
            await turn.output("speaker").send(
                SpeakerMatch(SpeakerStatus.MATCHED, speaker_id=tag_of(chunk.pcm), score=0.9)
            )
            return


class Resolve(Base):
    spec = SPECS["identity.resolve"]

    async def run_turn(self, turn: TurnSession) -> None:
        timeout_s = self.config.get("timeout_ms", 1500) / 1000
        match: SpeakerMatch | None = None
        try:
            async with asyncio.timeout(timeout_s):
                match = await turn.input("speaker").value()
        except TimeoutError:
            match = None
        if match is not None and match.status is SpeakerStatus.MATCHED:
            principal = Principal(
                user_id=match.speaker_id,
                roles=frozenset({"adult"}),
                assurance=Assurance.LOW,
                evidence=(Evidence("speaker", {"score": match.score}),),
            )
        else:
            principal = Principal.unknown()
        await turn.output("principal").send(principal)


class First(Base):
    spec = SPECS["text.first"]

    async def run_turn(self, turn: TurnSession) -> None:
        value = await turn.input("primary").value() or await turn.input("fallback").value()
        if value is not None:
            await turn.output("value").send(value)


class Agent(Base):
    spec = SPECS["ha.conversation"]

    async def run_turn(self, turn: TurnSession) -> None:
        if self.config["agent_id"] == "hang":
            await asyncio.Event().wait()
        principal = await turn.input("principal").value()
        utterance = await turn.input("utterance").value()
        if utterance is None:
            return
        who = principal.user_id if principal and principal.user_id else "unknown"
        count = (await turn.state.get("turns") or 0) + 1
        await turn.state.set("turns", count)
        await turn.output("response").send(
            Response(text=f"{who}: {utterance.text}", data={"turns": count})
        )


class Sink(Base):
    spec = SPECS["ha.assist.sink"]

    def __init__(self) -> None:
        self.results: dict[str, dict[str, Any]] = {}

    async def run_turn(self, turn: TurnSession) -> None:
        # A sink must survive a failed transcript: the response matters more.
        finals: list[str] = []
        try:
            finals = [t.text async for t in turn.input("transcript") if t.final]
        except StreamError:
            finals = []
        response = await turn.input("response").value()
        self.results[turn.context.turn_id] = {"finals": finals, "response": response}


def behaviour_registry() -> NodeRegistry:
    reg = NodeRegistry()
    classes: list[type[Node]] = [Source, Stt, Speaker, Resolve, First, Agent, Sink]
    for cls in classes:
        reg.register(cls)
    return reg
