"""Skills: checks, the tool definition, and filling the actions."""

from __future__ import annotations

import pytest

from flowpilot_sdk import Skill, SkillAction, SkillField
from flowpilot_sdk.skills import check, definition, render

TV = "media_player.tv"


def skill(*actions: SkillAction, fields: tuple[SkillField, ...] = ()) -> Skill:
    return Skill(
        "1", "tv", "Use the TV", fields or (SkillField("app", "", "choice", ("youtube",)),), actions
    )


SEARCH = Skill(
    "2",
    "tv_search",
    "Search YouTube on the TV",
    (SkillField("query", "What to look for"), SkillField("volume", type="number", required=False)),
    (
        SkillAction(
            "androidtv.adb_command",
            {"command": "am start -d https://www.youtube.com/results?search_query={query|url}"},
            {"entity_id": TV},
        ),
        SkillAction("media_player.volume_set", {"volume_level": "{volume}"}, {"entity_id": TV}),
    ),
)


def test_text_goes_into_commands_escaped() -> None:
    check(SEARCH)
    [search, volume] = render(SEARCH, {"query": "кот; rm -rf /", "volume": "0.3"})
    assert search.data["command"].endswith("search_query=%D0%BA%D0%BE%D1%82%3B%20rm%20-rf%20%2F")
    assert volume.data == {"volume_level": 0.3}  # a whole placeholder keeps its number
    shell = SkillAction("androidtv.adb_command", {"command": "input text {query|shell}"})
    [typed] = render(skill(shell, fields=(SkillField("query"),)), {"query": "a'b; ls"})
    assert typed.data["command"] == "input text 'a'\"'\"'b; ls'"


def test_unsafe_or_broken_skills_are_refused() -> None:
    raw = SkillAction("androidtv.adb_command", {"command": "input text {query}"})
    with pytest.raises(ValueError, match="escaped"):
        check(skill(raw, fields=(SkillField("query"),)))
    with pytest.raises(ValueError, match="not a field"):
        check(skill(SkillAction("script.turn_on", {"x": "{nope}"})))
    with pytest.raises(ValueError, match="needs options"):
        check(skill(SkillAction("script.turn_on"), fields=(SkillField("app", type="choice"),)))
    with pytest.raises(ValueError, match="domain.service"):
        check(skill(SkillAction("turn on")))


def test_values_are_checked_and_offered_to_the_model() -> None:
    choose = skill(SkillAction("androidtv.adb_command", {"command": "monkey -p {app} 1"}))
    assert render(choose, {"app": "youtube"})[0].data == {"command": "monkey -p youtube 1"}
    with pytest.raises(ValueError, match="one of"):
        render(choose, {"app": "evil; reboot"})
    with pytest.raises(ValueError, match="required"):
        render(SEARCH, {})
    tool = definition(SEARCH)["function"]
    assert tool["name"] == "skill__tv_search"
    assert tool["parameters"]["required"] == ["query"]
    assert tool["parameters"]["properties"]["volume"]["type"] == "number"
