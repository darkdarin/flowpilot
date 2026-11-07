"""NodeSpec validation and an example node written against the SDK interfaces."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from flowpilot_sdk import (
    IDENTITY_PROVIDER_GRANT,
    AudioChunk,
    AudioFormat,
    HostAudio,
    HostUnavailable,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    SessionInfo,
    SessionState,
    SourceInfo,
    SpeakerMatch,
    SpeakerStatus,
    StreamHeader,
    TurnContext,
    TurnSession,
    new_ulid,
)


def test_valid_spec() -> None:
    spec = NodeSpec(
        type="speaker.onnx",
        version="1.0.0",
        title="Speaker ID",
        category="identity",
        inputs={"audio": PortSpec("audio.pcm/1")},
        outputs={"speaker": PortSpec("identity.speaker/1", kind="value")},
    )
    assert spec.inputs["audio"].delivery == "lossless"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"type": "speaker"}, "node type"),
        ({"version": "1.0"}, "semver"),
        ({"inputs": {"Audio": PortSpec("audio.pcm/1")}}, "port name"),
        ({"turn_timeout_s": 0}, "positive"),
    ],
)
def test_invalid_spec(kwargs: dict[str, Any], match: str) -> None:
    base: dict[str, Any] = {
        "type": "x.node",
        "version": "1.0.0",
        "title": "X",
        "category": "logic",
    }
    with pytest.raises(ValueError, match=match):
        NodeSpec(**(base | kwargs))


def test_port_spec_validation() -> None:
    with pytest.raises(ValueError):
        PortSpec("not-a-type")
    with pytest.raises(ValueError):
        PortSpec("audio.pcm/1", buffer=0)


def test_privileged_output_requires_grant() -> None:
    outputs = {"principal": PortSpec("identity.principal/1", kind="value")}
    with pytest.raises(ValueError, match="requires grant"):
        NodeSpec(type="evil.node", version="1.0.0", title="E", category="logic", outputs=outputs)
    NodeSpec(
        type="identity.resolve",
        version="1.0.0",
        title="Resolve",
        category="identity",
        outputs=outputs,
        grants=frozenset({IDENTITY_PROVIDER_GRANT}),
    )


def test_ulid_shape_and_order() -> None:
    a = new_ulid(1_000)
    b = new_ulid(2_000)
    assert len(a) == 26 and a < b
    assert new_ulid() != new_ulid()
    with pytest.raises(ValueError):
        new_ulid(-1)


# --- example node against in-memory stream fakes --------------------------------------------------


class FakeInput:
    def __init__(self, header: StreamHeader | None, items: list[Any]) -> None:
        self._header = header
        self._items = items
        self.closed = False

    @property
    def connected(self) -> bool:
        return True

    async def header(self) -> StreamHeader | None:
        return self._header

    async def _iter(self) -> AsyncIterator[Any]:
        for item in self._items:
            if self.closed:
                return
            yield item

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter()

    async def value(self) -> Any | None:
        return self._items[0] if self._items else None

    def close(self) -> None:
        self.closed = True


class FakeOutput:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.ended = False

    async def open(self, meta: Mapping[str, Any] | None = None) -> None:
        return None

    async def send(self, payload: Any) -> None:
        self.items.append(payload)

    async def end(self) -> None:
        self.ended = True


class MemoryState:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class FakeTurn:
    def __init__(self, inputs: dict[str, FakeInput]) -> None:
        now = datetime.now(UTC)
        self.context = TurnContext(
            turn_id=new_ulid(),
            session_id=new_ulid(),
            started_at=now,
            deadline_mono_ns=0,
            source=SourceInfo(kind="test", adapter_id="test"),
            trace_id=new_ulid(),
            initial_principal=Principal.unknown(),
        )
        self.session = SessionInfo(session_id=self.context.session_id, created_at=now, turn_index=0)
        self.state: SessionState = MemoryState()
        self.inputs = inputs
        self.outputs: dict[str, FakeOutput] = {}
        self.events: list[str] = []

    def input(self, name: str) -> FakeInput:
        return self.inputs[name]

    def output(self, name: str) -> FakeOutput:
        return self.outputs.setdefault(name, FakeOutput())

    def trace(self, event: str, **attrs: Any) -> None:
        self.events.append(event)

    async def call_host(
        self,
        op: str,
        args: Mapping[str, Any],
        *,
        audio_in: AsyncIterable[bytes] | None = None,
        audio_format: AudioFormat | None = None,
    ) -> Any:
        raise HostUnavailable(op, "no client in this test")

    def stream_host(
        self, op: str, args: Mapping[str, Any]
    ) -> AbstractAsyncContextManager[HostAudio]:
        raise HostUnavailable(op, "no client in this test")


class WindowSpeakerId:
    """Collects a short speech window, closes its input early and reports a match."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="speaker.example",
        version="1.0.0",
        title="Example speaker ID",
        category="identity",
        inputs={"audio": PortSpec("audio.pcm/1")},
        outputs={"speaker": PortSpec("identity.speaker/1", kind="value")},
        isolation="subprocess",
        turn_timeout_s=2.0,
    )

    def __init__(self, window_s: float = 0.1) -> None:
        self.window_s = window_s

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        audio = turn.input("audio")
        header = await audio.header()
        if header is None:
            await turn.output("speaker").send(SpeakerMatch.unknown("no_audio"))
            return
        fmt = AudioFormat.from_meta(header.meta)
        need = int(fmt.sample_rate * self.window_s) * fmt.bytes_per_sample
        got = 0
        async for chunk in audio:
            got += len(chunk.pcm)
            if got >= need:
                break
        audio.close()
        await asyncio.sleep(0)  # stands in for model inference
        await turn.output("speaker").send(
            SpeakerMatch(status=SpeakerStatus.MATCHED, speaker_id="family.adult", score=0.9)
        )

    async def stop(self) -> None:
        return None


async def test_example_node_closes_input_early() -> None:
    fmt = AudioFormat(sample_rate=16000)
    chunk = 320 * fmt.bytes_per_sample  # 20 ms
    items = [AudioChunk(pcm=bytes(chunk), sample_offset=i * 320) for i in range(50)]  # 1 s
    audio = FakeInput(StreamHeader("audio.pcm/1", fmt.as_meta()), items)
    turn = FakeTurn({"audio": audio})

    await WindowSpeakerId(window_s=0.1).run_turn(turn)

    assert audio.closed
    result = turn.outputs["speaker"].items[0]
    assert result.status is SpeakerStatus.MATCHED


async def test_example_node_text_only_turn() -> None:
    turn = FakeTurn({"audio": FakeInput(None, [])})
    await WindowSpeakerId().run_turn(turn)
    assert turn.outputs["speaker"].items[0].reason == "no_audio"


def test_translations_must_match_ports_and_settings() -> None:
    from flowpilot_sdk import FieldText, NodeText

    base: dict[str, Any] = {
        "type": "test.texts",
        "version": "1.0.0",
        "title": "Texts",
        "category": "logic",
        "inputs": {"text": PortSpec("text.delta/1", description="Text in.")},
        "config_schema": {"type": "object", "properties": {"mode": {"type": "string"}}},
    }
    ok = NodeText(title="Тексты", inputs={"text": "Текст."}, config={"mode": FieldText("Режим")})
    assert NodeSpec(**base, translations={"ru": ok}).translations["ru"].title == "Тексты"
    with pytest.raises(ValueError, match="unknown inputs"):
        NodeSpec(**base, translations={"ru": NodeText(inputs={"audio": "Звук."})})
    with pytest.raises(ValueError, match="unknown outputs"):
        NodeSpec(**base, translations={"ru": NodeText(outputs={"text": "Текст."})})
    with pytest.raises(ValueError, match="unknown settings"):
        NodeSpec(**base, translations={"ru": NodeText(config={"speed": FieldText("Скорость")})})
    with pytest.raises(ValueError, match="language code"):
        NodeSpec(**base, translations={"Russian": ok})


def test_node_texts_round_trip_and_reject_unknown_keys() -> None:
    from flowpilot_sdk import NodeText

    data = {
        "title": "T",
        "inputs": {"a": "A"},
        "config": {"x": {"description": "X"}},
        "groups": {"g": {"title": "G"}},
    }
    assert NodeText.from_dict(data).as_dict() == data
    with pytest.raises(ValueError, match="unknown keys"):
        NodeText.from_dict({"titel": "typo"})


def test_settings_groups_must_be_declared() -> None:
    from flowpilot_sdk import GROUP_KEY, GROUPS_KEY, FieldText, NodeText

    def spec(schema: dict[str, Any], **texts: Any) -> NodeSpec:
        translations = {"ru": NodeText(**texts)} if texts else {}
        return NodeSpec(
            "test.groups",
            "1.0.0",
            "Groups",
            "logic",
            config_schema=schema,
            translations=translations,
        )

    grouped = {"type": "object", "properties": {"a": {"type": "string", GROUP_KEY: "g"}}}
    with pytest.raises(ValueError, match="unknown group 'g'"):
        spec(grouped)
    declared = grouped | {GROUPS_KEY: {"g": {"title": "G"}}}
    assert spec(declared, groups={"g": FieldText("Г")}).translations["ru"].groups["g"].title == "Г"
    with pytest.raises(ValueError, match="unknown groups"):
        spec(declared, groups={"h": FieldText("Х")})
