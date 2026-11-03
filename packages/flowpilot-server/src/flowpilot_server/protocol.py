"""FlowPilot client protocol, version 1 (ADR-014).

One WebSocket per client at ``/api/ws``. Text frames are JSON objects with a ``type``; binary frames
carry audio: a 4-byte big-endian stream id followed by raw PCM (``s16le``). Stream ids are chosen by
the side that sends the stream, so client and server ids never clash in one direction.

Client → server

- ``hello``          ``{protocol, token, client: {name, version}, host_ops: [op, ...]}`` — must be
  first; answered by ``hello.ok`` or ``error`` + close.
- ``turn.start``     ``{ref, graph?, source: {kind, device_id?, satellite_id?, area?}, language?,
  external?: {key: value}, user_id?, input: {audio: {stream, format}} | {utterance: {text}}}``
  — answered by ``turn.started {ref, turn_id, session_id}`` or ``error {ref}``.
- binary             audio for a client stream announced in ``turn.start``.
- ``stream.end``     ``{stream}`` — no more audio on that stream.
- ``turn.cancel``    ``{turn_id}``.
- ``host.result``    ``{call, ok: true, result}`` or ``{call, ok: false, error}``.

Server → client

- ``hello.ok``       ``{protocol, server: {version}, graphs: [id, ...], default_graph,
  missing_host_ops: {graph: [op, ...]}}``
- ``turn.started``   see above.
- ``turn.transcript`` ``{turn_id, text, final}``
- ``turn.delta``     ``{turn_id, text}`` — streamed response text.
- ``turn.response``  ``{turn_id, response: {text, speech_text, language, continue_conversation,
  data}}``
- ``stream.start``   ``{turn_id, stream, kind: "speech", format}`` then binary frames, then
  ``stream.end {stream}``.
- ``host.call``      ``{call, turn_id, op, args}`` — the client answers with ``host.result``.
- ``turn.end``       ``{turn_id, status: ok|degraded|timeout|cancelled, nodes: {id: status}}``
- ``error``          ``{code, message, ref?}``
"""

from __future__ import annotations

import struct

PROTOCOL_VERSION = 1
WS_PATH = "/api/ws"

_STREAM_ID = struct.Struct(">I")
MAX_STREAM_ID = 2**32 - 1


class ProtocolError(Exception):
    """A client message violates the protocol."""

    def __init__(self, code: str, message: str, ref: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.ref = ref


def pack_audio(stream: int, pcm: bytes) -> bytes:
    return _STREAM_ID.pack(stream) + pcm


def unpack_audio(frame: bytes) -> tuple[int, bytes]:
    if len(frame) < _STREAM_ID.size:
        raise ProtocolError("bad_frame", "binary frame shorter than the stream id")
    (stream,) = _STREAM_ID.unpack_from(frame)
    return stream, frame[_STREAM_ID.size :]
