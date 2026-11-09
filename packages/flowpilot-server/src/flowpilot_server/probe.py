"""The Debug probe: shows what reaches it on the node in live debug, and keeps its recordings.

A debug tap: it runs only while someone watches the graph in live debug, so the household's words
and voices reach the trace and the disk only when you look.

Its one input takes any type (``ANY``) and is wired next to the regular consumers of an output (an
output feeds several inputs); it reads the data as it comes, so it never holds a turn up:

- audio is saved as one WAV per turn and node, written in a thread when the stream ends, in the
  runtime's data under ``nodes/debug.probe/``, named ``<UTC time>.<graph>.<node>.<turn>.wav``; the
  oldest are deleted beyond the node's ``keep``. Recordings stay on this machine: traces carry only
  the file name, the editor plays them through its authenticated API, and the app keeps them out of
  backups. Reported as ``record.saved``.
- text is reported as ``tap.text`` (port, type, text, items; partial transcripts are counted), and
  anything else as its fields in JSON, kept only in the live debug history.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from flowpilot_sdk import (
    ANY,
    TEXT,
    AudioFormat,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    StreamError,
    TurnSession,
    node_translations,
    type_kind,
)

TYPE = "debug.probe"
PORT = "value"
AUDIO = "audio.pcm/1"
TEXT_TYPES = frozenset(TEXT.split("|"))
MAX_CHARS = 2000
MAX_ITEMS = 20  # of a stream of other data: shown in JSON, one line each
FILE_RE = re.compile(
    r"^(?P<time>\d{8}T\d{6}Z)\.(?P<graph>[a-z0-9][a-z0-9_-]*)\.(?P<node>[a-z0-9][a-z0-9_]*)"
    r"\.(?P<turn>[A-Za-z0-9]+)\.wav$"
)


def recordings_dir(data_dir: Path) -> Path:
    return data_dir / "nodes" / TYPE


def _cut(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def _plain(value: Any) -> Any:
    """A payload as JSON-friendly data: dataclass fields, bytes as their size."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=False)


class DebugProbe:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="3.0.0",
        title="Debug probe",
        category="debug",
        debug_tap=True,
        inputs={
            PORT: PortSpec(
                ANY,
                required=False,
                description=(
                    "Anything: audio is saved to play back (one recording per turn), text is shown "
                    "(recognized speech with the number of partial results, a text stream joined), "
                    "other data as its fields."
                ),
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "keep": {
                    "title": "Recordings to keep",
                    "description": "Older recordings of this node are deleted; 50 when not set.",
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 1000,
                },
                "max_seconds": {
                    "title": "Longest recording, s",
                    "description": "Audio after this is not saved; 30 when not set.",
                    "type": "number",
                    "default": 30,
                    "exclusiveMinimum": 0,
                    "maximum": 600,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=660.0,
        description=(
            "For debugging: connect it next to any output to see what went through, right on the "
            "node for the selected turn in live debug: audio to play back, text and other data to "
            "read. Works only while live debug is on; recordings stay on this machine."
        ),
    )

    def __init__(self) -> None:
        self._dir: Path | None = None
        self._graph = ""
        self._node = ""
        self._keep = 50
        self._max_seconds = 30.0

    async def start(self, ctx: NodeContext) -> None:
        self._keep = int(ctx.config.get("keep", 50))
        self._max_seconds = float(ctx.config.get("max_seconds", 30.0))
        self._graph, self._node = ctx.graph_id, ctx.node_id
        self._dir = ctx.data_dir
        if self._dir is None:
            ctx.logger.warning("this runtime has no data directory: nothing will be recorded")
        else:
            await asyncio.to_thread(self._dir.mkdir, parents=True, exist_ok=True)

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        stream = turn.input(PORT)
        if not stream.connected:
            return
        try:
            header = await stream.header()
        except StreamError as err:
            turn.trace("tap.text", port=PORT, error=err.reason)
            return
        if header is None:
            return
        if header.type == AUDIO:
            await self._record(turn, stream, header.meta)
        else:
            await self._show(turn, stream, header.type)

    async def _show(self, turn: TurnSession, stream: InputStream[Any], type_id: str) -> None:
        # which kind of data came: the editor shows it next to the text
        attrs: dict[str, Any] = {"port": PORT, "type": type_id}
        try:
            if type_kind(type_id) == "value":
                value = await stream.value()
                if value is None:
                    return
                text = value.text if type_id in TEXT_TYPES else _json(value)
                attrs |= {"text": _cut(text), "items": 1}
            elif type_id == "text.transcript/1":
                final, last, partials, items = "", "", 0, 0
                async for item in stream:
                    items += 1
                    last = item.text
                    if item.final:
                        final = item.text
                    else:
                        partials += 1
                attrs |= {"text": _cut(final or last), "items": items, "partials": partials}
            elif type_id in TEXT_TYPES:
                pieces = [item.text async for item in stream]
                attrs |= {"text": _cut("".join(pieces)), "items": len(pieces)}
            else:
                lines, items = [], 0
                async for item in stream:  # keep reading: the source must not wait for us
                    items += 1
                    if items <= MAX_ITEMS:
                        lines.append(_json(item))
                if items > MAX_ITEMS:
                    lines.append(f"… {items - MAX_ITEMS} more")
                attrs |= {"text": _cut("\n".join(lines)), "items": items}
        except StreamError as err:
            attrs |= {"error": err.reason}
        turn.trace("tap.text", **attrs)

    async def _record(
        self, turn: TurnSession, audio: InputStream[Any], meta: Mapping[str, Any]
    ) -> None:
        fmt = AudioFormat.from_meta(meta)
        frame = fmt.bytes_per_sample
        limit = int(self._max_seconds * fmt.sample_rate) * frame
        pcm = bytearray()
        try:
            async for chunk in audio:  # keep reading: the source must not wait for us
                if len(pcm) < limit:
                    pcm += chunk.pcm[: limit - len(pcm)]
        except StreamError:
            pass  # save what came before the source failed
        pcm = pcm[: len(pcm) - len(pcm) % frame]
        if not pcm or self._dir is None:
            return
        name = await asyncio.to_thread(self._save, turn.context.turn_id, fmt, bytes(pcm))
        seconds = round(len(pcm) / (fmt.sample_rate * frame), 2)
        turn.trace("record.saved", file=name, seconds=seconds)

    def _save(self, turn_id: str, fmt: AudioFormat, pcm: bytes) -> str:
        assert self._dir is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}.{self._graph}.{self._node}.{turn_id}.wav"
        tmp = self._dir / f".{name}.tmp"
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(fmt.channels)
            wav.setsampwidth(2)
            wav.setframerate(fmt.sample_rate)
            wav.writeframes(pcm)
        tmp.replace(self._dir / name)
        mine = sorted(
            p
            for p in self._dir.glob(f"*.{self._graph}.{self._node}.*.wav")
            if FILE_RE.match(p.name)
        )
        for old in mine[: max(0, len(mine) - self._keep)]:
            old.unlink(missing_ok=True)
        return name


@dataclass(frozen=True, slots=True)
class Recording:
    file: str
    graph: str
    node: str
    turn_id: str
    created: str
    seconds: float
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "graph": self.graph,
            "node": self.node,
            "turn_id": self.turn_id,
            "created": self.created,
            "seconds": self.seconds,
            "bytes": self.bytes,
        }


def list_recordings(directory: Path, graph: str | None = None) -> list[Recording]:
    """Recordings, newest first; only files this node wrote (the name pattern) are listed."""
    found = []
    for path in directory.glob("*.wav") if directory.is_dir() else []:
        m = FILE_RE.match(path.name)
        if m is None or (graph is not None and m["graph"] != graph):
            continue
        try:
            with wave.open(str(path), "rb") as wav:
                seconds = wav.getnframes() / wav.getframerate()
        except wave.Error, EOFError, OSError:
            continue
        created = datetime.strptime(m["time"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        found.append(
            Recording(
                path.name,
                m["graph"],
                m["node"],
                m["turn"],
                created.isoformat(),
                round(seconds, 2),
                path.stat().st_size,
            )
        )
    return sorted(found, key=lambda r: r.file, reverse=True)


def recording_path(directory: Path, name: str) -> Path | None:
    """The file behind a recording name, or ``None`` for anything that is not one."""
    if not FILE_RE.match(name):
        return None
    path = directory / name
    return path if path.is_file() else None
