"""What every node passed on in a turn someone watches in live debug, for the editor's journal.

The runtime shows each output of a watched turn to ``TurnCapture`` (``flowpilot_core.runtime
.taps``). When an output ends, its data goes to the trace as one ``port.data`` event:

- text as text: a text stream joined, recognized speech as its final text with the number of
  partial results, an utterance as its text;
- audio as a WAV recording in the runtime's data, named
  ``<UTC time>.<graph>.<node>-<port>.<turn>.wav``; recordings of the graph's last ``KEEP_TURNS``
  watched turns are kept. Recordings stay on this machine: the trace carries only the file name,
  the editor plays them through its authenticated API, and the app keeps them out of backups;
- anything else (an agent's answer, who speaks, ...) as JSON.

An output that passes on what another one sent (First present, a condition) is marked ``same`` as
that one, and its audio is not recorded twice. Only watched turns are captured: the household's
words and voices reach the trace and the disk only while someone looks.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flowpilot_core.runtime import TraceEvent, TraceSink
from flowpilot_sdk import AudioFormat, TurnContext

_LOGGER = logging.getLogger(__name__)

EVENT = "port.data"
AUDIO = "audio.pcm/1"
TRANSCRIPT = "text.transcript/1"
DELTA = "text.delta/1"
UTTERANCE = "text.utterance/1"
KEEP_TURNS = 20
MAX_SECONDS = 120.0
MAX_CHARS = 20000
MAX_ITEMS = 50  # of a stream of other data
# recordings: <UTC time>.<graph>.<node>[-<port>].<turn>.wav (the port since the journal; the Debug
# probe's recordings had none)
FILE_RE = re.compile(
    r"^(?P<time>\d{8}T\d{6}Z)\.(?P<graph>[a-z0-9][a-z0-9_-]*)\.(?P<node>[a-z0-9][a-z0-9_]*)"
    r"(?:-(?P<port>[a-z0-9_]+))?\.(?P<turn>[A-Za-z0-9]+)\.wav$"
)


def recordings_dir(data_dir: Path) -> Path:
    # where the Debug probe kept its recordings: the app already keeps it out of backups
    return data_dir / "nodes" / "debug.probe"


def plain(value: Any) -> Any:
    """A payload as JSON-friendly data: dataclass fields, bytes as their size."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _cut(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


@dataclass
class _Port:
    type_id: str
    meta: dict[str, Any]
    items: int = 0
    same: str | None = None  # it passes on what this output ("node.port") sent
    pieces: list[str] = field(default_factory=list)
    final: str | None = None
    partials: int = 0
    pcm: bytearray = field(default_factory=bytearray)
    values: list[Any] = field(default_factory=list)


class TurnCapture:
    """One watched turn's outputs; see the module docstring."""

    def __init__(
        self, sink: TraceSink, context: TurnContext, graph_id: str, directory: Path | None
    ) -> None:
        self._sink = sink
        self._turn_id = context.turn_id
        self._graph = graph_id
        self._dir = directory
        self._ports: dict[tuple[str, str], _Port] = {}
        # the first payload of each output, by identity: an output whose first payload is the very
        # object another one sent passes it on (kept alive so ids stay unique)
        self._first: dict[int, tuple[str, Any]] = {}
        self._saving: list[asyncio.Task[None]] = []

    # --- OutputTap -------------------------------------------------------------------------------

    def opened(self, node_id: str, port: str, type_id: str, meta: Mapping[str, Any]) -> None:
        self._ports[(node_id, port)] = _Port(type_id, dict(meta))

    def item(self, node_id: str, port: str, payload: Any) -> None:
        out = self._ports.get((node_id, port))
        if out is None:
            return
        out.items += 1
        if out.items == 1:
            seen = self._first.get(id(payload))
            if seen is not None and seen[1] is payload:
                out.same = seen[0]
            else:
                self._first[id(payload)] = (f"{node_id}.{port}", payload)
        if out.type_id == AUDIO:
            if out.same is None:
                fmt = AudioFormat.from_meta(out.meta)
                limit = int(MAX_SECONDS * fmt.sample_rate) * fmt.bytes_per_sample
                if len(out.pcm) < limit:
                    out.pcm += payload.pcm[: limit - len(out.pcm)]
        elif out.type_id == TRANSCRIPT:
            out.pieces = [payload.text]
            if payload.final:
                out.final = payload.text
            else:
                out.partials += 1
        elif out.type_id == DELTA:
            out.pieces.append(payload.text)
        elif out.type_id == UTTERANCE:
            out.pieces = [payload.text]
        elif len(out.values) < MAX_ITEMS:
            out.values.append(plain(payload))

    def ended(self, node_id: str, port: str, state: str) -> None:
        out = self._ports.pop((node_id, port), None)
        if out is None or not out.items:
            return
        attrs: dict[str, Any] = {"port": port, "type": out.type_id, "items": out.items}
        attrs["state"] = state
        if out.same is not None:
            attrs["same"] = out.same
        if out.type_id == AUDIO:
            if out.same is None and out.pcm and self._dir is not None:
                self._saving.append(asyncio.ensure_future(self._record(node_id, out, attrs)))
                return
        elif out.type_id in (TRANSCRIPT, DELTA, UTTERANCE):
            attrs["text"] = _cut(out.final if out.final is not None else "".join(out.pieces))
            if out.type_id == TRANSCRIPT:
                attrs["partials"] = out.partials
        else:
            data = out.values[0] if out.items == 1 and out.values else out.values
            if out.items > len(out.values):
                attrs["more"] = out.items - len(out.values)
            attrs["json"] = _cut(json.dumps(data, ensure_ascii=False))
        self._emit(node_id, attrs)

    async def close(self) -> None:
        if self._saving:
            await asyncio.gather(*self._saving, return_exceptions=True)
        if self._dir is not None:
            await asyncio.to_thread(prune, self._dir, self._graph, KEEP_TURNS)

    # --- recordings ------------------------------------------------------------------------------

    async def _record(self, node_id: str, out: _Port, attrs: dict[str, Any]) -> None:
        fmt = AudioFormat.from_meta(out.meta)
        frame = fmt.bytes_per_sample
        pcm = bytes(out.pcm[: len(out.pcm) - len(out.pcm) % frame])
        try:
            name = await asyncio.to_thread(self._save, node_id, str(attrs["port"]), fmt, pcm)
        except OSError as err:
            _LOGGER.warning("could not save the recording of %s: %s", node_id, err)
            attrs["error"] = f"not recorded: {err}"
        else:
            attrs |= {"file": name, "seconds": round(len(pcm) / (fmt.sample_rate * frame), 2)}
        self._emit(node_id, attrs)

    def _save(self, node_id: str, port: str, fmt: AudioFormat, pcm: bytes) -> str:
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"{stamp}.{self._graph}.{node_id}-{port}.{self._turn_id}.wav"
        tmp = self._dir / f".{name}.tmp"
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(fmt.channels)
            wav.setsampwidth(2)
            wav.setframerate(fmt.sample_rate)
            wav.writeframes(pcm)
        tmp.replace(self._dir / name)
        return name

    def _emit(self, node_id: str, attrs: dict[str, Any]) -> None:
        self._sink.emit(TraceEvent(self._turn_id, EVENT, node_id, attrs=attrs))


def prune(directory: Path, graph: str, keep_turns: int) -> None:
    """Delete the graph's recordings beyond its last ``keep_turns`` turns."""
    turns: dict[str, list[Path]] = {}
    for path in directory.glob(f"*.{graph}.*.wav") if directory.is_dir() else []:
        m = FILE_RE.match(path.name)
        if m is not None and m["graph"] == graph:
            turns.setdefault(m["turn"], []).append(path)
    newest = sorted(turns, key=lambda t: max(p.name for p in turns[t]), reverse=True)
    for turn in newest[keep_turns:]:
        for path in turns[turn]:
            path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Recording:
    file: str
    graph: str
    node: str
    port: str | None
    turn_id: str
    created: str
    seconds: float
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def list_recordings(directory: Path, graph: str | None = None) -> list[Recording]:
    """Recordings, newest first; only files the runtime wrote (the name pattern) are listed."""
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
                m["port"],
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
