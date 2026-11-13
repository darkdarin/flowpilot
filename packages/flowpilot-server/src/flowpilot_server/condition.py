"""``logic.condition``: route data one way or the other by conditions.

What comes on ``value`` (any type) goes out on ``yes`` or ``no`` (the same type), never both. The
conditions are settings, combined with "all" or "any":

- about the turn, known at once: voice or text, the time of day and the days of the week, the
  area of the speaker's device (Home Assistant names it; in a voice turn only once speech is
  recognized, so put such a condition after speech-to-text);
- about what comes on the optional ``check`` input (any type, often the same output as
  ``value``): data came or not; for text, words in any of their forms or a regular expression,
  and its language; for who speaks (``identity.principal``), the people or whether they are known.

The language is what the text on check says of itself: speech recognition's (``Utterance``,
``Transcript``), an agent's answer (``Response``, or the header of its text stream, known as the
stream opens, before its words); the turn's language when the text does not say.

While the conditions are open the node keeps what comes on ``value`` and passes it on once the
branch is decided: a condition on a text stream waits for the whole text. Privileged: it may pass
who speaks on, so it holds ``identity.provider``; it only forwards what it got.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, ClassVar

from flowpilot_sdk import (
    ANY,
    ENUM_TITLES_KEY,
    IDENTITY_PROVIDER_GRANT,
    LOG_EVENT,
    OPTIONS_KEY,
    TEXT,
    WHEN_INPUT_KEY,
    HostError,
    HostUnavailable,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    StreamError,
    TurnSession,
    node_translations,
    type_kind,
)
from flowpilot_sdk.language import language_base, stream_language

from .names import _forms, words

TYPE = "logic.condition"
WHERE_OP = "ha.turn.where"
PRINCIPAL = "identity.principal/1"
DELTA = "text.delta/1"
TEXT_TYPES = TEXT.split("|")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_TITLES = {day: day.capitalize() for day in DAYS}
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
_END = object()

# the settings that are conditions (the rest say how to combine them)
TURN_CONDITIONS = ("turn", "time_from", "days", "areas")
CHECK_CONDITIONS = ("present", "words", "pattern", "languages", "people", "known")
# languages offered for the language condition (editors show their names)
LANGUAGES = [
    "ru", "en", "uk", "be", "kk", "de", "fr", "es", "it", "pt", "pl", "tr", "ar", "he", "zh",
    "ja", "ko",
]  # fmt: skip


def _when(types: list[str] | str) -> dict[str, Any]:
    return {WHEN_INPUT_KEY: {"port": "check", "types": types}}


class Condition:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Condition",
        category="logic",
        grants=frozenset({IDENTITY_PROVIDER_GRANT}),  # forwards who speaks when routed through it
        optional_host_ops=frozenset({WHERE_OP}),
        inputs={
            "value": PortSpec(ANY, var="T", description="What goes on: out on yes or on no."),
            "check": PortSpec(
                ANY,
                required=False,
                description=(
                    "What the conditions look at, when they look at data: anything (came or not), "
                    "text (words, a pattern, its language) or who speaks. Often the same output as "
                    "value."
                ),
            ),
        },
        outputs={
            "yes": PortSpec(ANY, var="T", description="The value when the conditions hold."),
            "no": PortSpec(ANY, var="T", description="The value when they do not."),
        },
        config_schema={
            "type": "object",
            "properties": {
                "match": {
                    "title": "Conditions",
                    "description": "Yes when all of them hold, or when any one does.",
                    "enum": ["all", "any"],
                    ENUM_TITLES_KEY: {"all": "All of them", "any": "Any of them"},
                    "default": "all",
                },
                "turn": {
                    "title": "Turn",
                    "description": "Spoken or typed.",
                    "enum": ["voice", "text"],
                    ENUM_TITLES_KEY: {"voice": "Spoken", "text": "Typed"},
                },
                "time_from": {
                    "title": "From",
                    "description": (
                        "From this time (HH:MM); with Until, a span that may pass midnight."
                    ),
                    "type": "string",
                    "format": "time",  # editors show a time field
                    "pattern": TIME_RE,
                },
                "time_to": {
                    "title": "Until",
                    "description": "Until this time (HH:MM), not included.",
                    "type": "string",
                    "format": "time",  # editors show a time field
                    "pattern": TIME_RE,
                },
                "days": {
                    "title": "Days",
                    "description": "On these days of the week.",
                    "type": "array",
                    "items": {"enum": DAYS},
                    ENUM_TITLES_KEY: DAY_TITLES,
                    "uniqueItems": True,
                },
                "areas": {
                    "title": "Areas",
                    "description": (
                        "The speaker's device is in one of these areas. In a voice turn Home "
                        "Assistant names the device once speech is recognized."
                    ),
                    "type": "array",
                    "items": {"type": "string", OPTIONS_KEY: "ha.area"},
                    "uniqueItems": True,
                },
                "present": {
                    "title": "Data on check",
                    "description": "Something came on check, or nothing did.",
                    "enum": ["yes", "no"],
                    ENUM_TITLES_KEY: {"yes": "Came", "no": "Did not come"},
                    **_when("*"),
                },
                "words": {
                    "title": "Words",
                    "description": (
                        'The text has one of these words or phrases, in any form ("включи" '
                        'matches "включить").'
                    ),
                    "type": "array",
                    "items": {"type": "string"},
                    **_when(TEXT_TYPES),
                },
                "pattern": {
                    "title": "Pattern",
                    "description": "The text matches this regular expression (case ignored).",
                    "type": "string",
                    **_when(TEXT_TYPES),
                },
                "languages": {
                    "title": "Language",
                    "description": (
                        "The text is in one of these languages: what speech recognition heard, "
                        "what an agent answers in (known as its answer begins); the turn's "
                        "language when the text does not say."
                    ),
                    "type": "array",
                    "items": {"enum": LANGUAGES, "format": "language"},
                    "uniqueItems": True,
                    **_when(TEXT_TYPES),
                },
                "people": {
                    "title": "People",
                    "description": "Who speaks is one of these people.",
                    "type": "array",
                    "items": {"type": "string", OPTIONS_KEY: "ha.user"},
                    "uniqueItems": True,
                    **_when([PRINCIPAL]),
                },
                "known": {
                    "title": "Known",
                    "description": "Whether who speaks is known at all.",
                    "enum": ["known", "unknown"],
                    ENUM_TITLES_KEY: {"known": "Known", "unknown": "Unknown"},
                    **_when([PRINCIPAL]),
                },
            },
            "additionalProperties": False,
        },
        description=(
            "Sends what comes in one way or the other: by the turn (spoken or typed, the time, the "
            "area) and by what comes on check (data or not, words in the text, its language, "
            "who speaks)."
        ),
    )

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._pattern: re.Pattern[str] | None = None

    async def start(self, ctx: NodeContext) -> None:
        self._config = dict(ctx.config)
        if pattern := self._config.get("pattern"):
            try:
                self._pattern = re.compile(str(pattern), re.IGNORECASE)
            except re.error as err:
                raise ValueError(f"the pattern is not a regular expression: {err}") from err

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        value: InputStream[Any] = turn.input("value")
        kept: asyncio.Queue[Any] = asyncio.Queue()  # what came while the branch was open
        keeper = asyncio.create_task(self._keep(value, kept))
        started = time.monotonic()
        try:
            branch, why = await self._decide(turn)
            waited_ms = round(1000 * (time.monotonic() - started))
            turn.trace(
                "logic.condition", branch="yes" if branch else "no", why=why, waited_ms=waited_ms
            )
            if turn.watched:
                turn.trace(
                    LOG_EVENT, kind="condition", summary=("yes" if branch else "no") + f": {why}",
                    ms=waited_ms,
                )  # fmt: skip
            await self._pass(turn.output("yes" if branch else "no"), value, kept)
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper

    # ---- value ------------------------------------------------------------------------------

    @staticmethod
    async def _keep(value: InputStream[Any], kept: asyncio.Queue[Any]) -> None:
        """Read value as it comes, so its source never waits for the decision."""
        try:
            header = await value.header()
            if header is None:
                return
            if type_kind(header.type) == "value":
                if (item := await value.value()) is not None:
                    await kept.put(item)
                return
            async for item in value:
                await kept.put(item)
        except StreamError as err:
            await kept.put(err)
        finally:
            await kept.put(_END)

    @staticmethod
    async def _pass(out: Any, value: InputStream[Any], kept: asyncio.Queue[Any]) -> None:
        header = await value.header()
        if header is None:
            return  # nothing came: neither branch carries anything
        await out.open(header.meta, type_id=header.type)
        while (item := await kept.get()) is not _END:
            if isinstance(item, StreamError):
                raise item  # its source failed: so does this branch
            await out.send(item)
        await out.end()

    # ---- conditions -------------------------------------------------------------------------

    async def _decide(self, turn: TurnSession) -> tuple[bool, str]:
        cfg = self._config
        tests: list[tuple[str, Callable[[], Any]]] = []
        if turn_kind := cfg.get("turn"):
            tests.append((turn_kind, lambda: self._turn_is(turn, turn_kind)))
        if cfg.get("time_from") or cfg.get("time_to") or cfg.get("days"):
            tests.append(("time", self._time_fits))
        if areas := cfg.get("areas"):
            tests.append(("area", lambda: self._in_area(turn, list(areas))))
        if any(cfg.get(key) for key in CHECK_CONDITIONS):
            seen: list[str] = []  # what the check saw, for the reason
            tests.append(("check", lambda: self._check_fits(turn, seen)))
        if not tests:
            return True, "no conditions"
        want_all = cfg.get("match", "all") != "any"
        reasons = []
        for name, test in tests:  # in order: the quick ones first, data last
            ok = test()
            if asyncio.iscoroutine(ok):
                ok = await ok
            detail = f" ({'; '.join(seen)})" if name == "check" and seen else ""
            reasons.append(f"{name} {'✓' if ok else '✗'}{detail}")
            if want_all and not ok:
                return False, ", ".join(reasons)
            if not want_all and ok:
                return True, ", ".join(reasons)
        return want_all, ", ".join(reasons)

    @staticmethod
    def _turn_is(turn: TurnSession, kind: str) -> bool:
        return turn.context.voice == (kind == "voice")

    def _time_fits(self, now: datetime | None = None) -> bool:
        now = now or datetime.now().astimezone()
        days = self._config.get("days")
        if days and DAYS[now.weekday()] not in days:
            return False
        start, end = self._config.get("time_from"), self._config.get("time_to")
        minute = now.hour * 60 + now.minute
        a = _minutes(start) if start else 0
        b = _minutes(end) if end else 24 * 60
        return a <= minute < b if a <= b else minute >= a or minute < b  # past midnight

    @staticmethod
    async def _in_area(turn: TurnSession, areas: list[str]) -> bool:
        device = turn.context.device
        args = {
            "device_id": device.device_id if device else None,
            "satellite_id": device.satellite_id if device else None,
        }
        try:
            where = await turn.call_host(WHERE_OP, args)
        except HostError, HostUnavailable:
            return False  # nobody can say where: not in the area
        area = where.get("area_id") if isinstance(where, Mapping) else None
        return area in areas

    async def _check_fits(self, turn: TurnSession, seen: list[str]) -> bool:
        cfg = self._config
        check: InputStream[Any] = turn.input("check")
        try:
            header = await check.header() if check.connected else None
        except StreamError:
            header = None
        results: list[bool] = []
        if present := cfg.get("present"):
            results.append((header is not None) == (present == "yes"))
        wants_text = bool(cfg.get("words") or cfg.get("pattern"))
        wanted_languages = list(cfg.get("languages") or [])
        wants_who = bool(cfg.get("people") or cfg.get("known"))
        if wants_text or wanted_languages:
            if header is not None and header.type not in TEXT_TYPES:
                raise ValueError(f"text conditions need text on check, not {header.type}")
            text: str | None = None
            said = stream_language(header) if header is not None else None
            # a stream that says its language in its header is decided without its words
            if header is not None and (wants_text or (said is None and header.type != DELTA)):
                text, of_items = await _text_of(check, header.type)
                said = said or of_items
            if wanted_languages:
                language = said or turn.context.language
                seen.append(f"language {language or '?'}" + ("" if said else " of the turn"))
                base = language_base(language)
                results.append(base is not None and base in wanted_languages)
            if wants_text and text is None:
                results.append(False)  # nothing to look at
            elif text is not None:
                if cfg.get("words"):
                    words_language = said or turn.context.language
                    results.append(_has_words(text, list(cfg["words"]), words_language))
                if self._pattern is not None:
                    results.append(self._pattern.search(text) is not None)
        if wants_who:
            if header is None:
                results.append(False)  # nothing to look at
            else:
                if header.type != PRINCIPAL:
                    raise ValueError(
                        f"conditions on who speaks need it on check, not {header.type}"
                    )
                who: Principal | None = await check.value()
                known = who is not None and who.is_known
                if cfg.get("known"):
                    results.append(known == (cfg["known"] == "known"))
                if cfg.get("people"):
                    results.append(known and who is not None and who.user_id in cfg["people"])
        want_all = cfg.get("match", "all") != "any"
        return all(results) if want_all else any(results)


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


async def _text_of(stream: InputStream[Any], type_id: str) -> tuple[str, str | None]:
    """The whole text on a text input, and its language when the text says."""
    if type_kind(type_id) == "value":
        item = await stream.value()
        return str(getattr(item, "text", "") or ""), getattr(item, "language", None)
    final, last, pieces, language = "", "", [], None
    async for item in stream:
        language = getattr(item, "language", None) or language
        text = str(getattr(item, "text", "") or "")
        if type_id == "text.transcript/1":
            last = text
            if getattr(item, "final", False):
                final = text
        else:
            pieces.append(text)
    return ((final or last) if type_id == "text.transcript/1" else "".join(pieces)), language


def _has_words(text: str, phrases: list[str], language: str | None) -> bool:
    """One of the phrases is in the text, each word in any of its forms."""
    base = (language or "").split("-")[0].casefold()
    tokens = [_forms(w, base) for w in words(text)]
    for phrase in phrases:
        wanted = [_forms(w, base) for w in words(phrase)]
        n = len(wanted)
        if n and any(
            all(wanted[k] & tokens[i + k] for k in range(n)) for i in range(len(tokens) - n + 1)
        ):
            return True
    return False
