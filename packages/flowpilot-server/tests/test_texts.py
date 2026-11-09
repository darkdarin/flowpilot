"""Every bundled node describes itself, its ports and settings, in English and Russian."""

from __future__ import annotations

import pytest

from flowpilot_core.conformance import missing_descriptions
from flowpilot_sdk import ENUM_TITLES_KEY, NodeSpec
from flowpilot_sdk.node import enum_values
from flowpilot_server.__main__ import build_registry

REGISTRY = build_registry(dev=True)
SPECS = [spec for t in REGISTRY.types() if (spec := REGISTRY.spec(t)) is not None]
LANGUAGES = ["ru"]


def test_all_plugins_are_loaded() -> None:
    assert {"wyoming.stt", "wyoming.tts", "vad.segmenter", "text.first"} <= set(REGISTRY.types())


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.type)
def test_english_texts(spec: NodeSpec) -> None:
    assert spec.title
    assert missing_descriptions(spec) == []
    for name, prop in (spec.config_schema.get("properties") or {}).items():
        assert prop.get("title"), f"setting {name} has no title"
        if enum_values(prop):  # every value of a list is labelled
            assert set(prop.get(ENUM_TITLES_KEY) or {}) == {str(v) for v in enum_values(prop)}, name


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.type)
def test_translations_are_complete(spec: NodeSpec, language: str) -> None:
    text = spec.translations.get(language)
    assert text is not None, f"no {language} texts"
    assert text.title and text.description
    assert set(text.inputs) == set(spec.inputs)
    assert set(text.outputs) == set(spec.outputs)
    props = set(spec.config_schema.get("properties") or {})
    assert set(text.config) == props
    for name, field in text.config.items():
        assert field.title and field.description, f"setting {name}"
        values = {str(v) for v in enum_values(spec.config_schema["properties"][name])}
        assert set(field.options) == values, f"labels of {name}"


def test_catalog_carries_translations() -> None:
    from flowpilot_server.manager import GraphManager

    catalog = GraphManager(REGISTRY).catalog()
    stt = next(n for n in catalog["nodes"] if n["type"] == "wyoming.stt")
    assert stt["inputs"][0]["description"] == "Speech to recognize."
    assert stt["config_schema"]["properties"]["uri"]["title"] == "Service"
    ru = stt["translations"]["ru"]
    assert ru["title"] == "Распознавание речи Wyoming"
    assert ru["inputs"] == {"audio": "Речь для распознавания."}
    assert ru["config"]["uri"]["title"] == "Сервис"
