"""Latency benchmark: an Assist pipeline with a Wyoming STT directly vs. the same STT via FlowPilot.

Pipeline A ("direct"): Home Assistant Wyoming integration → Wyoming STT, Home Assistant agent.
Pipeline B ("flowpilot"): FlowPilot STT entity → graph (wyoming.stt → same service →
ha.conversation → Home Assistant agent) → FlowPilot conversation entity.

Audio is a WAV file (16 kHz mono s16le) played at real-time pace with leading silence and trailing
silence until the pipeline reports the end of speech-to-text. Runs alternate A/B.

Measured per run (milliseconds):
- ``stt``: from the end of speech in the audio to ``stt-end``
- ``agent``: from ``stt-end`` to ``intent-end``
- ``total``: from the end of speech to ``intent-end``

Run with the Python of the Home Assistant venv (needs aiohttp)::

    python -I scripts/latency_ha.py --audio phrase.wav --wyoming tcp://127.0.0.1:10300 --runs 10
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import statistics
import time
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

ROOT = Path.home() / "Projects" / "ha-dev"
BASE = os.environ.get("HA_URL", "http://127.0.0.1:8123")
SECRETS = Path(os.environ.get("HA_SECRETS_DIR", ROOT))
TOKEN = (SECRETS / ".ha-token").read_text().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
FRAME_MS = 20
FRAME_BYTES = 16000 * 2 * FRAME_MS // 1000
LEAD_SILENCE_MS = 500
MAX_TAIL_MS = 6000


class Ws:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.ids = itertools.count(1)
        self.queues: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for msg in self.ws:
            data = msg.json()
            if (q := self.queues.get(data.get("id"))) is not None:
                q.put_nowait(data)

    async def start(self, payload: dict[str, Any]) -> asyncio.Queue[dict[str, Any]]:
        msg_id = next(self.ids)
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.queues[msg_id] = q
        await self.ws.send_json({"id": msg_id, **payload})
        return q

    async def call(self, payload: dict[str, Any]) -> Any:
        res = await (await self.start(payload)).get()
        if not res.get("success"):
            raise RuntimeError(res)
        return res["result"]


def load_speech(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit("audio must be 16 kHz mono 16-bit")
        return w.readframes(w.getnframes())


async def ensure_wyoming_entry(http: aiohttp.ClientSession, uri: str) -> None:
    resp = await http.get(f"{BASE}/api/config/config_entries/entry", headers=HEADERS)
    if any(e["domain"] == "wyoming" for e in await resp.json()):
        return
    parsed = urlparse(uri)
    flow = await (
        await http.post(
            f"{BASE}/api/config/config_entries/flow", json={"handler": "wyoming"}, headers=HEADERS
        )
    ).json()
    result = await (
        await http.post(
            f"{BASE}/api/config/config_entries/flow/{flow['flow_id']}",
            json={"host": parsed.hostname, "port": parsed.port},
            headers=HEADERS,
        )
    ).json()
    print("wyoming entry:", result.get("type"), result.get("errors") or "")
    await asyncio.sleep(3)


async def stt_entities(ws: Ws) -> dict[str, str]:
    entities = await ws.call({"type": "config/entity_registry/list"})
    found: dict[str, str] = {}
    for e in entities:
        domain = e["entity_id"].split(".")[0]
        if e["platform"] in ("wyoming", "flowpilot") and domain in ("stt", "conversation"):
            found[f"{e['platform']}.{domain}"] = e["entity_id"]
    return found


async def ensure_pipeline(ws: Ws, name: str, stt: str, agent: str, language: str) -> str:
    listing = await ws.call({"type": "assist_pipeline/pipeline/list"})
    for p in listing["pipelines"]:
        if p["name"] == name:
            return str(p["id"])
    created = await ws.call(
        {
            "type": "assist_pipeline/pipeline/create",
            "name": name,
            "language": language,
            "conversation_engine": agent,
            "conversation_language": language,
            "stt_engine": stt,
            "stt_language": language,
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    return str(created["id"])


async def run_once(ws: Ws, pipeline_id: str, speech: bytes) -> dict[str, Any]:
    q = await ws.start(
        {
            "type": "assist_pipeline/run",
            "start_stage": "stt",
            "end_stage": "intent",
            "input": {"sample_rate": 16000},
            "pipeline": pipeline_id,
        }
    )
    marks: dict[str, float] = {}
    stt_done = asyncio.Event()
    result: dict[str, Any] = {}

    async def send(handler: int) -> None:
        prefix = bytes([handler])
        frames = [bytes(FRAME_BYTES)] * (LEAD_SILENCE_MS // FRAME_MS)
        frames += [speech[i : i + FRAME_BYTES] for i in range(0, len(speech), FRAME_BYTES)]
        start = time.monotonic()
        for i, frame in enumerate(frames):
            await ws.ws.send_bytes(prefix + frame)
            await asyncio.sleep(max(0.0, start + (i + 1) * FRAME_MS / 1000 - time.monotonic()))
        marks["speech_end"] = time.monotonic()
        for _ in range(MAX_TAIL_MS // FRAME_MS):  # trailing silence until STT is done
            if stt_done.is_set():
                break
            await ws.ws.send_bytes(prefix + bytes(FRAME_BYTES))
            await asyncio.sleep(FRAME_MS / 1000)
        await ws.ws.send_bytes(prefix)

    sender: asyncio.Task[None] | None = None
    try:
        while True:
            msg = await asyncio.wait_for(q.get(), 30)
            event = msg.get("event")
            if not event:
                continue
            kind, data = event["type"], event.get("data") or {}
            if kind == "run-start":
                sender = asyncio.create_task(send(data["runner_data"]["stt_binary_handler_id"]))
            elif kind == "stt-end":
                marks["stt_end"] = time.monotonic()
                result["text"] = data["stt_output"]["text"]
                stt_done.set()
            elif kind == "intent-end":
                marks["intent_end"] = time.monotonic()
                result["answer"] = data["intent_output"]["response"]["speech"]["plain"]["speech"]
            elif kind == "error":
                result["error"] = data
                stt_done.set()
            if kind in ("run-end", "error"):
                break
    finally:
        if sender is not None:
            await asyncio.gather(sender, return_exceptions=True)
    if "speech_end" in marks and "stt_end" in marks and "intent_end" in marks:
        result["stt"] = (marks["stt_end"] - marks["speech_end"]) * 1000
        result["agent"] = (marks["intent_end"] - marks["stt_end"]) * 1000
        result["total"] = (marks["intent_end"] - marks["speech_end"]) * 1000
    return result


def summary(values: list[float]) -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return f"median {statistics.median(values):7.1f}  p90 {p90:7.1f}  min {ordered[0]:7.1f}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--wyoming", default="tcp://127.0.0.1:10300")
    parser.add_argument("--language", default="ru")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--json", type=Path, help="write raw results here")
    args = parser.parse_args()
    speech = load_speech(args.audio)

    async with aiohttp.ClientSession() as http:
        await ensure_wyoming_entry(http, args.wyoming)
        async with http.ws_connect(f"{BASE}/api/websocket") as raw:
            await raw.receive_json()
            await raw.send_json({"type": "auth", "access_token": TOKEN})
            assert (await raw.receive_json())["type"] == "auth_ok"
            ws = Ws(raw)
            ent = await stt_entities(ws)
            pipelines = {
                "direct": await ensure_pipeline(
                    ws, "Bench: Wyoming direct", ent["wyoming.stt"],
                    "conversation.home_assistant", args.language,
                ),
                "flowpilot": await ensure_pipeline(
                    ws, "Bench: FlowPilot", ent["flowpilot.stt"],
                    ent["flowpilot.conversation"], args.language,
                ),
            }  # fmt: skip
            await run_once(ws, pipelines["direct"], speech)  # warm-up, not counted
            await run_once(ws, pipelines["flowpilot"], speech)
            results: dict[str, list[dict[str, Any]]] = {"direct": [], "flowpilot": []}
            for i in range(args.runs):
                for name in ("direct", "flowpilot") if i % 2 == 0 else ("flowpilot", "direct"):
                    r = await run_once(ws, pipelines[name], speech)
                    results[name].append(r)
                    print(f"{name:9} run {i + 1:2}: {r.get('text')!r} -> {r.get('answer')!r} "
                          f"stt={r.get('stt', 0):.0f} agent={r.get('agent', 0):.0f} "
                          f"total={r.get('total', 0):.0f} {r.get('error') or ''}")  # fmt: skip
                    await asyncio.sleep(0.5)

    print()
    for name, runs in results.items():
        ok = [r for r in runs if "total" in r]
        print(f"{name} ({len(ok)}/{len(runs)} ok)")
        for metric in ("stt", "agent", "total"):
            print(f"  {metric:5} {summary([r[metric] for r in ok])}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, ensure_ascii=False))


asyncio.run(main())
