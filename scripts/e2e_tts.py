"""End-to-end check of the FlowPilot text-to-speech entity against a running Home Assistant.

Deploys a graph ``speak`` to the runtime (fixed-phrase STT, Fixed answer, the beeping dev TTS),
runs a voice turn through an Assist pipeline that uses FlowPilot speech-to-text, agent and
text-to-speech, and downloads the speech the way a satellite does (the TTS proxy URL). The audio
must be the graph's: 10 ms of a constant tone per character of the answer (ADR-017).

Run it with the Python of the Home Assistant venv (it needs aiohttp), ``ffmpeg`` on PATH and a
FlowPilot runtime started with ``--dev``. Environment as for ``e2e_ha.py``.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path.home() / "Projects" / "ha-dev"
BASE = os.environ.get("HA_URL", "http://127.0.0.1:8123")
SECRETS = Path(os.environ.get("HA_SECRETS_DIR", ROOT))
TOKEN = (SECRETS / ".ha-token").read_text().strip()
FP_URL = os.environ.get("FLOWPILOT_URL", "http://127.0.0.1:8765")
FP_TOKEN = (
    Path(os.environ.get("FLOWPILOT_TOKEN_FILE", ROOT / "flowpilot" / "token")).read_text().strip()
)
CHUNK = bytes(16000 * 2 // 50)
PHRASE = "what time is it"
LONG = " and that is all there is to it, said FlowPilot to the satellite"

GRAPH: dict[str, Any] = {
    "schema_version": 1,
    "id": "speak",
    "name": "Speak",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "stt", "type": "dev.fixed_stt", "config": {"text": PHRASE, "max_chunks": 10}},
        {"id": "agent", "type": "debug.answer", "config": {"text": "echo: {text}"}},
        {"id": "voice", "type": "dev.beep_tts"},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.audio", "to": "stt.audio"},
        {"from": "stt.transcript", "to": "out.transcript"},
        {"from": "stt.utterance", "to": "agent.utterance"},
        {"from": "agent.delta", "to": "out.delta"},
        {"from": "agent.delta", "to": "voice.text"},
        {"from": "agent.response", "to": "out.response"},
        {"from": "voice.speech", "to": "out.speech"},
    ],
}


async def deploy(http: aiohttp.ClientSession, answer: str) -> None:
    headers = {"Authorization": f"Bearer {FP_TOKEN}"}
    api = f"{FP_URL}/api/editor"
    graphs = (await (await http.get(f"{api}/graphs", headers=headers)).json())["graphs"]
    if not any(g["id"] == "speak" for g in graphs):
        resp = await http.post(
            f"{api}/graphs", json={"id": "speak", "name": "Speak"}, headers=headers
        )
        resp.raise_for_status()
    doc = {**GRAPH, "nodes": [dict(n) for n in GRAPH["nodes"]]}
    doc["nodes"][2] = {**doc["nodes"][2], "config": {"text": answer}}
    (await http.put(f"{api}/graphs/speak/draft", json=doc, headers=headers)).raise_for_status()
    (await http.post(f"{api}/graphs/speak/deploy", headers=headers)).raise_for_status()


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


async def pipeline(ws: Ws) -> str:
    name = "FlowPilot speaks"
    for p in (await ws.call({"type": "assist_pipeline/pipeline/list"}))["pipelines"]:
        if p["name"] == name:
            return str(p["id"])
    created = await ws.call(
        {
            "type": "assist_pipeline/pipeline/create",
            "name": name,
            "language": "en",
            "conversation_engine": "conversation.flowpilot_speak",
            "conversation_language": "en",
            "stt_engine": "stt.flowpilot_speak",
            "stt_language": "en",
            "tts_engine": "tts.flowpilot_speak",
            "tts_language": "en",
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    return str(created["id"])


async def voice_turn(ws: Ws, pipeline_id: str) -> tuple[str, str]:
    """Run STT → intent → TTS; return the answer and the TTS URL."""
    q = await ws.start(
        {
            "type": "assist_pipeline/run",
            "start_stage": "stt",
            "end_stage": "tts",
            "input": {"sample_rate": 16000},
            "pipeline": pipeline_id,
        }
    )
    answer = url = ""
    sender: asyncio.Task[None] | None = None

    async def send_audio(handler: int) -> None:
        prefix = bytes([handler])
        for _ in range(250):
            await ws.ws.send_bytes(prefix + CHUNK)
            await asyncio.sleep(0.02)
        await ws.ws.send_bytes(prefix)

    try:
        while True:
            msg = await asyncio.wait_for(q.get(), 30)
            event = msg.get("event")
            if not event:
                continue
            kind, data = event["type"], event.get("data") or {}
            if kind == "run-start":
                sender = asyncio.create_task(
                    send_audio(data["runner_data"]["stt_binary_handler_id"])
                )
            elif kind == "intent-end":
                answer = data["intent_output"]["response"]["speech"]["plain"]["speech"]
            elif kind == "tts-end":
                url = data["tts_output"]["url"]
            elif kind == "error":
                raise RuntimeError(data)
            if kind == "run-end":
                break
    finally:
        if sender is not None:
            sender.cancel()
    return answer, url


def pcm16(audio: bytes) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        input=audio,
        capture_output=True,
        check=True,
    ).stdout


async def check(http: aiohttp.ClientSession, ws: Ws, pipeline_id: str, expected: str) -> bool:
    answer, url = await voice_turn(ws, pipeline_id)
    audio = await (await http.get(f"{BASE}{url}")).read()
    pcm = pcm16(audio)
    samples = len(pcm) // 2
    want = 160 * len(answer)
    level = sorted(
        abs(int.from_bytes(pcm[i : i + 2], "little", signed=True)) for i in range(0, len(pcm), 2)
    )
    median = level[len(level) // 2] if level else 0
    ok = answer == expected and abs(samples - want) < 0.1 * want + 1600 and 800 < median < 1200
    print(
        f"  answer={answer!r} ({len(answer)} chars) audio={len(audio)} B {url.rsplit('.', 1)[-1]}"
    )
    print(f"  samples={samples} expected≈{want} median level={median}  -> {'OK' if ok else 'FAIL'}")
    return ok


async def main() -> int:
    ok = True
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"{BASE}/api/websocket") as raw:
            await raw.receive_json()
            await raw.send_json({"type": "auth", "access_token": TOKEN})
            assert (await raw.receive_json())["type"] == "auth_ok"
            ws = Ws(raw)
            for name, answer in (("short", "echo: {text}"), ("streamed", "echo: {text}" + LONG)):
                await deploy(http, answer)
                for _ in range(50):  # the entities appear once HA learns the graph
                    states = await ws.call({"type": "get_states"})
                    if any(s["entity_id"] == "tts.flowpilot_speak" for s in states):
                        break
                    await asyncio.sleep(0.2)
                pipeline_id = await pipeline(ws)
                expected = answer.replace("{text}", PHRASE)
                print(f"== {name} answer")
                ok &= await check(http, ws, pipeline_id, expected)
                print(f"== {name} answer again (Home Assistant's cache)")
                ok &= await check(http, ws, pipeline_id, expected)
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
