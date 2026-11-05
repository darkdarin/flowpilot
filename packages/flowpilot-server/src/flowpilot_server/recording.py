"""Debug recordings: a node that saves the audio on a wire, and the files it keeps.

A debug tap: it records only while someone watches the graph in live debug.

``debug.record`` is wired to any audio output next to the regular consumer (an output feeds several
inputs), so recording never holds a turn up: the audio is read as it comes and written to disk in a
thread when the stream ends. One WAV per turn and node, in the runtime's data under
``nodes/debug.record/``, named ``<UTC time>.<graph>.<node>.<turn>.wav``; the oldest are deleted
beyond the node's ``keep``. Recordings stay on this machine: traces carry only the file name, the
editor plays them through its authenticated API, and the app keeps them out of backups.
"""

from __future__ import annotations

import asyncio
import re
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from flowpilot_sdk import (
    AudioFormat,
    NodeContext,
    NodeSpec,
    PortSpec,
    StreamError,
    TurnSession,
    node_translations,
)

TYPE = "debug.record"
FILE_RE = re.compile(
    r"^(?P<time>\d{8}T\d{6}Z)\.(?P<graph>[a-z0-9][a-z0-9_-]*)\.(?P<node>[a-z0-9][a-z0-9_]*)"
    r"\.(?P<turn>[A-Za-z0-9]+)\.wav$"
)


def recordings_dir(data_dir: Path) -> Path:
    return data_dir / "nodes" / TYPE


class RecordAudio:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Record audio",
        category="debug",
        debug_tap=True,
        inputs={
            "audio": PortSpec(
                "audio.pcm/1",
                description=(
                    "Audio to save: connect it to any audio output next to the regular consumer."
                ),
            )
        },
        config_schema={
            "type": "object",
            "properties": {
                "keep": {
                    "title": "Recordings to keep",
                    "description": "Older recordings of this node are deleted; 50 when not set.",
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                },
                "max_seconds": {
                    "title": "Longest recording, s",
                    "description": "Audio after this is not saved; 30 when not set.",
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 600,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=660.0,
        description=(
            "For debugging: saves the audio of each turn to a WAV file on the runtime, to play "
            "back in live debug. Works only while live debug is on; recordings stay on this "
            "machine."
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
        audio = turn.input("audio")
        header = await audio.header()
        if header is None:
            return
        fmt = AudioFormat.from_meta(header.meta)
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
