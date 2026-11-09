"""Entity options for the editor, repair issues for missing references, spoken errors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components import stt, tts
from homeassistant.components.conversation import async_converse
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.conversation import spoken_error
from custom_components.flowpilot.host import build_host_ops
from custom_components.flowpilot.references import ISSUE_PREFIX, ReferenceMonitor

from .conftest import TOKEN, Runtime, _runtime, setup_flowpilot

AGENT_ENTITY = "conversation.flowpilot_agent"
pytestmark = pytest.mark.usefixtures("socket_enabled")  # tests talk to a real runtime


async def test_entities_list(hass: HomeAssistant) -> None:
    hass.states.async_set("stt.cloud", "unknown", {"friendly_name": "Cloud STT"})
    hass.states.async_set("tts.piper", "unknown", {"friendly_name": "Piper"})
    hass.states.async_set("conversation.home_assistant", "unknown")
    registry = er.async_get(hass)
    own = registry.async_get_or_create("stt", "flowpilot", "entry-stt")
    hass.states.async_set(own.entity_id, "unknown")

    wyoming = MockConfigEntry(
        domain="wyoming", title="faster-whisper", data={"host": "10.0.0.5", "port": 10300}
    )
    wyoming.add_to_hass(hass)
    registry.async_get_or_create("stt", "wyoming", "whisper", config_entry=wyoming)

    masha = await hass.auth.async_create_user("Masha")
    await hass.auth.async_create_system_user("Supervisor")

    result = await build_host_ops(hass, MockConfigEntry(domain="flowpilot"))["ha.entities.list"](
        HostRequest(None, {})
    )
    options = result["options"]
    assert options["ha.stt"] == [{"id": "stt.cloud", "name": "Cloud STT"}]  # own STT left out
    assert options["ha.tts"] == [{"id": "tts.piper", "name": "Piper"}]
    assert [o["id"] for o in options["ha.conversation"]] == ["conversation.home_assistant"]
    assert options["wyoming.asr"] == [{"id": "tcp://10.0.0.5:10300", "name": "faster-whisper"}]
    assert options["wyoming.tts"] == []
    assert result["strict"] == ["ha.stt", "ha.tts", "ha.conversation"]
    people = {o["id"]: o["name"] for o in options["ha.user"]}
    assert people[masha.id] == "Masha"  # people for the memory's facts; system users left out
    assert "Supervisor" not in people.values()


async def test_people_list(hass: HomeAssistant) -> None:
    pavel = await hass.auth.async_create_user("Pavel")
    masha = await hass.auth.async_create_user("Masha")
    await hass.auth.async_create_system_user("Supervisor")
    hass.states.async_set("person.pavel", "Work", {"friendly_name": "Павел", "user_id": pavel.id})
    hass.states.async_set("person.grisha", "home", {"friendly_name": "Гриша"})  # no login

    result = await build_host_ops(hass, MockConfigEntry(domain="flowpilot"))["ha.people.list"](
        HostRequest(None, {})
    )
    people = {p["name"]: p for p in result["people"]}
    assert people["Павел"] == {
        "name": "Павел",
        "user_id": pavel.id,
        "entity_id": "person.pavel",
        "state": "Work",
    }
    assert people["Гриша"]["user_id"] is None and people["Гриша"]["state"] == "home"
    # a user without a person is still someone the agent may talk to; Pavel is not listed twice
    assert people["Masha"] == {
        "name": "Masha",
        "user_id": masha.id,
        "entity_id": None,
        "state": None,
    }
    assert "Pavel" not in people and "Supervisor" not in people


def issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == "flowpilot" and issue_id.startswith(ISSUE_PREFIX)
    }


async def settle(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()


async def test_repair_issue_follows_missing_entities(
    hass: HomeAssistant, speech_runtime: Runtime
) -> None:
    """The speech graph uses stt.fake and tts.fake; neither exists in this Home Assistant."""
    await setup_flowpilot(hass, speech_runtime)
    await settle(hass)
    [issue] = issues(hass).values()
    assert issue.translation_key == "missing_references"
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["graph"] == "test"
    assert "stt.fake (node stt, entity_id)" in issue.translation_placeholders["references"]
    assert "tts.fake (node tts, engine)" in issue.translation_placeholders["references"]

    original = stt.async_get_speech_to_text_entity
    with (
        patch.object(
            stt,
            "async_get_speech_to_text_entity",
            side_effect=lambda h, e: object() if e == "stt.fake" else original(h, e),
        ),
        patch.object(tts, "async_resolve_engine", side_effect=lambda h, e: e),
    ):
        # any entity registry change triggers a check
        er.async_get(hass).async_get_or_create("sensor", "demo", "something")
        await settle(hass)
        assert issues(hass) == {}


async def test_monitor_removes_its_issues_when_stopped(hass: HomeAssistant) -> None:
    monitor = ReferenceMonitor(hass, "entry")
    monitor.start()
    monitor.update({"kids": [{"node": "agent", "field": "agent_id",
                              "kind": "ha.conversation", "id": "conversation.gone"},
                             {"node": "stt", "field": "uri", "kind": "wyoming.asr",
                              "id": "tcp://10.0.0.5:10300"}]})  # fmt: skip
    await settle(hass)
    [issue] = issues(hass).values()
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["references"] == (
        "- conversation.gone (node agent, agent_id)"  # Wyoming services are not HA's to check
    )
    monitor.stop()
    assert issues(hass) == {}


async def test_editor_gets_options_from_home_assistant(
    hass: HomeAssistant, speech_runtime: Runtime
) -> None:
    hass.states.async_set("stt.cloud", "unknown", {"friendly_name": "Cloud STT"})
    await setup_flowpilot(hass, speech_runtime)
    async with aiohttp.ClientSession() as session:
        resp = await session.get(
            f"{speech_runtime.url}/api/editor/options",
            params={"refresh": "1"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        body = await resp.json()
    assert body["available"] is True and body["strict"] == ["ha.conversation", "ha.stt", "ha.tts"]
    assert {"id": "stt.cloud", "name": "Cloud STT"} in body["options"]["ha.stt"]


@pytest.fixture
async def broken_agent_runtime() -> AsyncIterator[Runtime]:
    agent: dict[str, Any] = {"type": "ha.conversation", "config": {"agent_id": "conversation.gone"}}
    async for rt in _runtime(agent):
        yield rt


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("ru", "Не получилось ответить: в голосовом сценарии произошла ошибка."),
        ("en", "No answer: a step of the voice flow failed."),
    ],
)
async def test_missing_agent_is_explained_in_the_turn_language(
    hass: HomeAssistant, broken_agent_runtime: Runtime, language: str, expected: str
) -> None:
    await setup_flowpilot(hass, broken_agent_runtime)
    result = await async_converse(
        hass, "привет", None, Context(), language=language, agent_id=AGENT_ENTITY
    )
    assert result.response.speech["plain"]["speech"] == expected


def test_spoken_error_falls_back_to_english() -> None:
    assert spoken_error("timeout", "ru-RU") == "FlowPilot не успел ответить."
    assert spoken_error("timeout", "de") == "FlowPilot did not answer in time."
    assert spoken_error("unknown-reason", None) == "No answer: a step of the voice flow failed."


async def test_where_the_turn_comes_from(hass: HomeAssistant) -> None:
    from homeassistant.helpers import area_registry as ar  # noqa: PLC0415
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

    kitchen = ar.async_get(hass).async_create("Кухня")
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "speaker")}, name="Колонка"
    )
    dr.async_get(hass).async_update_device(device.id, area_id=kitchen.id)
    satellite = er.async_get(hass).async_get_or_create(
        "assist_satellite", "test", "speaker", device_id=device.id, config_entry=entry
    )
    ops = build_host_ops(hass, MockConfigEntry(domain="flowpilot"))

    # the satellite has no area of its own: its device's counts
    where = await ops["ha.turn.where"](HostRequest(None, {"satellite_id": satellite.entity_id}))
    assert where["area_id"] == kitchen.id and where["area"] == "Кухня"
    assert where["device_id"] == device.id
    nowhere = await ops["ha.turn.where"](HostRequest(None, {}))
    assert nowhere["area_id"] is None
    options = (await ops["ha.entities.list"](HostRequest(None, {})))["options"]
    assert {"id": kitchen.id, "name": "Кухня"} in options["ha.area"]
