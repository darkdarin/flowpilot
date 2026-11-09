"""``identity.speaker``: who speaks, by their voice, standing in the ``principal`` wire.

It takes who the client says asks (the client input's ``principal``) and the turn's audio. When
the client already names a person (a chat, the Home Assistant app, Telegram), that is passed on as
it is. Otherwise the first seconds of speech are compared with the household's voice profiles
(``NodeContext.voices``) while speech-to-text goes on in parallel: a match gives that person, at
``Assurance.LOW`` (a voice is a convenience, not proof: a recording can fool it); no match, an
error or no time left gives an unknown person. ``match`` carries the raw result for live debug and
for a Condition.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

from flowpilot_sdk import (
    IDENTITY_PROVIDER_GRANT,
    Assurance,
    AudioFormat,
    Evidence,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    SpeakerMatch,
    SpeakerStatus,
    StreamError,
    TurnSession,
    Voices,
    node_translations,
)

TYPE = "identity.speaker"
PRINCIPAL = "identity.principal/1"


class SpeakerId:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Who speaks",
        category="identity",
        grants=frozenset({IDENTITY_PROVIDER_GRANT}),
        inputs={
            "audio": PortSpec(
                "audio.pcm/1", required=False, description="The turn's speech, as it comes."
            ),
            "principal": PortSpec(
                PRINCIPAL,
                kind="value",
                required=False,
                description=(
                    "Who the client says asks (the client input's); a known person is passed on "
                    "as is, the voice is not checked."
                ),
            ),
        },
        outputs={
            "principal": PortSpec(
                PRINCIPAL,
                kind="value",
                description=(
                    "Who speaks: the client's person, else the one whose voice matched, else "
                    "unknown. Wire it where the client input's went."
                ),
            ),
            "match": PortSpec(
                "identity.speaker/1",
                kind="value",
                description="The voice check itself: who it was closest to and how close.",
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "seconds": {
                    "title": "Speech to listen to, s",
                    "description": (
                        "How much of the turn's speech to compare; more is surer, but answers "
                        "later. 3 when not set."
                    ),
                    "type": "number",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                },
                "timeout_s": {
                    "title": "Longest wait, s",
                    "description": (
                        "Unknown if the check is not done this long after the audio began; 6."
                    ),
                    "type": "number",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 6,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=60.0,
        description=(
            "Tells who speaks by their voice, against the voice profiles of the household (the "
            "Voices page): a convenience, not proof. Put it in the principal wire."
        ),
    )

    def __init__(self) -> None:
        self._voices: Voices | None = None
        self._seconds = 3.0
        self._timeout_s = 6.0

    async def start(self, ctx: NodeContext) -> None:
        self._voices = ctx.voices
        self._seconds = float(ctx.config.get("seconds", 3))
        self._timeout_s = float(ctx.config.get("timeout_s", 6))

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        heard = _Heard(turn.input("audio"), self._seconds)
        drain = asyncio.create_task(heard.drain())  # read it all: the audio's source must not wait
        try:
            await self._identify(turn, heard)
        finally:
            await drain

    async def _identify(self, turn: TurnSession, heard: _Heard) -> None:
        client: Principal | None = None
        if turn.input("principal").connected:
            client = await turn.input("principal").value()
        if client is not None and client.is_known:
            # the client names who asks (a chat, the app): no voice to check
            await turn.output("principal").send(client)
            return
        started = time.monotonic()
        try:
            match = await asyncio.wait_for(self._check(heard), self._timeout_s)
        except TimeoutError:
            match = SpeakerMatch(SpeakerStatus.TIMEOUT, reason="no time left")
        turn.trace(
            "identity.speaker",
            status=match.status.value,
            score=match.score,
            threshold=match.threshold,
            ms=round(1000 * (time.monotonic() - started)),
            **({"speaker": match.speaker_id, "reason": match.reason} if turn.watched else {}),
        )
        await turn.output("match").send(match)
        if match.status is SpeakerStatus.MATCHED and match.speaker_id:
            principal = Principal(
                user_id=match.speaker_id,
                roles=frozenset(),
                assurance=Assurance.LOW,  # a voice alone never says more
                evidence=(Evidence("speaker", {"score": match.score, "model": match.model_id}),),
            )
        else:
            principal = Principal.unknown(Evidence("speaker", {"status": match.status.value}))
        await turn.output("principal").send(principal)

    async def _check(self, heard: _Heard) -> SpeakerMatch:
        if self._voices is None:
            return SpeakerMatch(SpeakerStatus.ERROR, reason="this runtime keeps no voices")
        await heard.enough.wait()
        if heard.problem:
            return SpeakerMatch.unknown(heard.problem)
        return await self._voices.identify(heard.speech(), heard.sample_rate)


class _Heard:
    """The turn's audio, read to its end; ``enough`` once the speech to compare is in."""

    def __init__(self, audio: InputStream[Any], seconds: float) -> None:
        self._audio = audio
        self._seconds = seconds
        self._pcm = bytearray()
        self._wanted = 0
        self.sample_rate = 16000
        self.problem: str | None = None
        self.enough = asyncio.Event()

    def speech(self) -> bytes:
        return bytes(self._pcm[: self._wanted])

    async def drain(self) -> None:
        try:
            header = await self._audio.header() if self._audio.connected else None
            if header is None:
                self.problem = "no speech in this turn"
                return
            fmt = AudioFormat.from_meta(header.meta)
            if fmt.channels != 1:
                self.problem = "mono speech only"
            self.sample_rate = fmt.sample_rate
            self._wanted = int(self._seconds * fmt.sample_rate) * fmt.bytes_per_sample
            async for chunk in self._audio:
                if len(self._pcm) < self._wanted:
                    self._pcm += chunk.pcm
                elif not self.enough.is_set():
                    self.enough.set()
        except StreamError:
            pass
        finally:
            self.enough.set()  # the audio ended: what came is all there is
