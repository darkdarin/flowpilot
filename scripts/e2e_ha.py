"""End-to-end check of the FlowPilot integration against a running Home Assistant.

Runs two text turns in one conversation and one voice turn through an Assist pipeline that uses
the FlowPilot speech-to-text and conversation entities (checks ADR-012 correlation live).

Run it with the Python of the Home Assistant venv (it needs aiohttp) and a FlowPilot runtime
serving a graph with ``dev.fixed_stt`` (``flowpilot-server <config> --dev``).

Environment:
- HA_URL (default http://127.0.0.1:8123), HA_SECRETS_DIR: directory with ``.ha-token``
- FLOWPILOT_URL (default http://127.0.0.1:8765), FLOWPILOT_TOKEN_FILE
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path.home() / "Projects" / "ha-dev"
BASE = os.environ.get("HA_URL", "http://127.0.0.1:8123")
SECRETS = Path(os.environ.get("HA_SECRETS_DIR", ROOT))
TOKEN = (SECRETS / ".ha-token").read_text().strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
FP_URL = os.environ.get("FLOWPILOT_URL", "http://127.0.0.1:8765")
FP_TOKEN = (
    Path(os.environ.get("FLOWPILOT_TOKEN_FILE", ROOT / "flowpilot" / "token")).read_text().strip()
)
CHUNK = bytes(16000 * 2 // 50)  # 20 ms of silence


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

    async def start(self, payload: dict[str, Any]) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        msg_id = next(self.ids)
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.queues[msg_id] = q
        await self.ws.send_json({"id": msg_id, **payload})
        return msg_id, q

    async def call(self, payload: dict[str, Any]) -> Any:
        _, q = await self.start(payload)
        res = await q.get()
        if not res.get("success"):
            raise RuntimeError(res)
        return res["result"]


async def ensure_entry(http: aiohttp.ClientSession) -> None:
    entries = await (
        await http.get(f"{BASE}/api/config/config_entries/entry", headers=HEADERS)
    ).json()
    if any(e["domain"] == "flowpilot" for e in entries):
        return
    flow = await (
        await http.post(
            f"{BASE}/api/config/config_entries/flow", json={"handler": "flowpilot"}, headers=HEADERS
        )
    ).json()
    result = await (
        await http.post(
            f"{BASE}/api/config/config_entries/flow/{flow['flow_id']}",
            json={"url": FP_URL, "token": FP_TOKEN},
            headers=HEADERS,
        )
    ).json()
    print("config entry:", result.get("type"), result.get("errors"))
    await asyncio.sleep(2)


async def entity_ids(ws: Ws) -> dict[str, str]:
    entities = await ws.call({"type": "config/entity_registry/list"})
    return {
        e["entity_id"].split(".")[0]: e["entity_id"]
        for e in entities
        if e["platform"] == "flowpilot"
    }


async def ensure_pipeline(ws: Ws, stt_id: str, agent_id: str) -> str:
    listing = await ws.call({"type": "assist_pipeline/pipeline/list"})
    for p in listing["pipelines"]:
        if p["name"] == "FlowPilot":
            return str(p["id"])
    created = await ws.call(
        {
            "type": "assist_pipeline/pipeline/create",
            "name": "FlowPilot",
            "language": "en",
            "conversation_engine": agent_id,
            "conversation_language": "en",
            "stt_engine": stt_id,
            "stt_language": "en",
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    return str(created["id"])


async def voice_turn(ws: Ws, pipeline_id: str) -> dict[str, Any]:
    _, q = await ws.start(
        {
            "type": "assist_pipeline/run",
            "start_stage": "stt",
            "end_stage": "intent",
            "input": {"sample_rate": 16000},
            "pipeline": pipeline_id,
        }
    )
    events: list[tuple[float, str, Any]] = []
    sender: asyncio.Task[None] | None = None
    started = time.monotonic()

    async def send_audio(handler: int) -> None:
        prefix = bytes([handler])
        for _ in range(250):  # up to 5 s; the dev STT ends earlier
            await ws.ws.send_bytes(prefix + CHUNK)
            await asyncio.sleep(0.02)
        await ws.ws.send_bytes(prefix)

    try:
        while True:
            msg = await asyncio.wait_for(q.get(), 30)
            event = msg.get("event")
            if not event:
                if msg.get("type") == "result" and not msg.get("success"):
                    raise RuntimeError(msg)
                continue
            events.append((round(time.monotonic() - started, 3), event["type"], event.get("data")))
            if event["type"] == "run-start":
                sender = asyncio.create_task(
                    send_audio(event["data"]["runner_data"]["stt_binary_handler_id"])
                )
            if event["type"] in ("run-end", "error"):
                break
    finally:
        if sender is not None:
            sender.cancel()
    summary = {
        "stt": next((d["stt_output"]["text"] for _, t, d in events if t == "stt-end"), None),
        "deltas": [d for _, t, d in events if t == "intent-progress"],
        "speech": next(
            (
                d["intent_output"]["response"]["speech"]["plain"]["speech"]
                for _, t, d in events
                if t == "intent-end"
            ),
            None,
        ),
        "timeline": [(t, k) for t, k, _ in events],
        "errors": [d for _, k, d in events if k == "error"],
    }
    return summary


async def main() -> None:
    async with aiohttp.ClientSession() as http:
        await ensure_entry(http)
        async with http.ws_connect(f"{BASE}/api/websocket") as raw:
            await raw.receive_json()
            await raw.send_json({"type": "auth", "access_token": TOKEN})
            assert (await raw.receive_json())["type"] == "auth_ok"
            ws = Ws(raw)
            ids = await entity_ids(ws)
            print("entities:", ids)

            print("== text turn x2 (same conversation)")
            conv_id = None
            for _ in range(2):
                res = await ws.call(
                    {
                        "type": "conversation/process",
                        "text": "what time is it",
                        "agent_id": ids["conversation"],
                        "language": "en",
                        **({"conversation_id": conv_id} if conv_id else {}),
                    }
                )
                conv_id = res["conversation_id"]
                print(" ", json.dumps(res["response"]["speech"]["plain"]["speech"]), conv_id)

            print("== voice turn via assist_pipeline/run")
            pipeline_id = await ensure_pipeline(ws, ids["stt"], ids["conversation"])
            print(json.dumps(await voice_turn(ws, pipeline_id), indent=1, ensure_ascii=False))


asyncio.run(main())
