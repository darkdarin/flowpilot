"""Node interface: static spec, instance lifecycle and the per-turn session."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from .context import SessionInfo, SessionState, TurnContext
from .host import HostAudio
from .payloads import AudioFormat
from .streams import InputStream, OutputStream
from .text import LANGUAGE_RE, NodeText
from .types import PRIVILEGED_TYPES, validate_type_id

NODE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

Category = Literal["input", "speech", "identity", "logic", "agent", "tool", "output", "debug"]
Isolation = Literal["in_process", "subprocess", "any"]


@dataclass(frozen=True, slots=True)
class PortSpec:
    """A typed port of a node."""

    type: str
    kind: Literal["stream", "value"] = "stream"
    required: bool = True
    delivery: Literal["lossless", "latest"] = "lossless"
    buffer: int = 32
    description: str = ""  # English; translations in NodeSpec.translations

    def __post_init__(self) -> None:
        validate_type_id(self.type)
        if self.buffer < 1:
            raise ValueError("port buffer must be at least 1")


# JSON Schema keyword for a config property that names something a host provides, e.g.
# ``{"type": "string", OPTIONS_KEY: "ha.stt"}``. Editors offer the host's list for it and Home
# Assistant raises a repair issue when a deployed graph refers to something that is gone.
# Kinds: ``ha.stt``, ``ha.tts``, ``ha.conversation`` (entity ids), ``wyoming.asr``,
# ``wyoming.tts`` (``tcp://host:port`` of services Home Assistant knows).
OPTIONS_KEY = "x-flowpilot-options"

# JSON Schema keyword (``true``) for a config property whose choices the node lists itself from the
# rest of its config, e.g. the models of the Wyoming service in ``uri``. The node class then has
#
#     @classmethod
#     async def config_options(cls, field: str, config: Mapping[str, Any]) -> list[ConfigOption]
#
# which editors call while the node is being configured (no instance, no turn). Return what fits
# the given config, or ``[]`` when it cannot tell (for example the service is unreachable).
# Language settings carry ``"format": "language"`` so editors show language names for the codes.
NODE_OPTIONS_KEY = "x-flowpilot-node-options"

# JSON Schema keyword with English labels for the values of an ``enum`` setting:
# ``{"enum": ["whole", "words"], ENUM_TITLES_KEY: {"whole": "At once", "words": "Word by word"}}``.
# Translations carry the same under ``config.<setting>.options``.
ENUM_TITLES_KEY = "x-flowpilot-enum-titles"


@dataclass(frozen=True, slots=True)
class ConfigOption:
    """One choice for a config setting: the value, and how to show it."""

    id: str
    name: str = ""  # human-readable; editors fall back to ``id``
    description: str = ""


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """Static declaration of a node type."""

    type: str
    version: str
    title: str
    category: Category
    inputs: Mapping[str, PortSpec] = field(default_factory=dict)
    outputs: Mapping[str, PortSpec] = field(default_factory=dict)
    config_schema: Mapping[str, Any] = field(default_factory=lambda: {"type": "object"})
    isolation: Isolation = "any"
    grants: frozenset[str] = frozenset()
    turn_timeout_s: float | None = None
    host_ops: frozenset[str] = frozenset()  # host operations the node may call (ADR-014)
    description: str = ""
    # Texts in other languages (see flowpilot_sdk.text); English is title/description above.
    translations: Mapping[str, NodeText] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for op in self.host_ops:
            if not NODE_TYPE_RE.match(op):
                raise ValueError(f"invalid host operation name {op!r} in {self.type}")
        if not NODE_TYPE_RE.match(self.type):
            raise ValueError(f"invalid node type {self.type!r}, expected 'namespace.name'")
        if not SEMVER_RE.match(self.version):
            raise ValueError(f"invalid node version {self.version!r}, expected semver 'X.Y.Z'")
        for name in (*self.inputs, *self.outputs):
            if not PORT_NAME_RE.match(name):
                raise ValueError(f"invalid port name {name!r} in {self.type}")
        for name, port in self.outputs.items():
            grant = PRIVILEGED_TYPES.get(port.type)
            if grant is not None and grant not in self.grants:
                raise ValueError(
                    f"{self.type}: output {name!r} of privileged type {port.type!r} "
                    f"requires grant {grant!r}"
                )
        if self.turn_timeout_s is not None and self.turn_timeout_s <= 0:
            raise ValueError("turn_timeout_s must be positive")
        settings = set(self.config_schema.get("properties") or {})
        for language, text in self.translations.items():
            if not LANGUAGE_RE.match(language):
                raise ValueError(f"{self.type}: invalid language code {language!r}")
            where = f"{self.type} ({language})"
            if unknown := set(text.inputs) - set(self.inputs):
                raise ValueError(f"{where}: translations for unknown inputs {sorted(unknown)}")
            if unknown := set(text.outputs) - set(self.outputs):
                raise ValueError(f"{where}: translations for unknown outputs {sorted(unknown)}")
            if unknown := set(text.config) - settings:
                raise ValueError(f"{where}: translations for unknown settings {sorted(unknown)}")
            for name, field_text in text.config.items():
                values = {str(v) for v in self.config_schema["properties"][name].get("enum", [])}
                if unknown := set(field_text.options) - values:
                    raise ValueError(f"{where}: labels for unknown values of {name}: {unknown}")


class NodeContext(Protocol):
    """What a node instance gets at start: its identity in the graph, config and secrets."""

    node_id: str
    graph_id: str
    config: Mapping[str, Any]
    logger: logging.Logger
    # A directory of this node type in the runtime's data, kept across turns and restarts (files a
    # node keeps: recordings, enrolled voices). ``None`` when the runtime has no storage.
    data_dir: Path | None

    def secret(self, name: str) -> str:
        """Value of a secret referenced in the node config; ``KeyError`` if not granted."""
        ...


class TurnSession(Protocol):
    """What a node sees during one turn."""

    context: TurnContext
    session: SessionInfo
    state: SessionState

    def input(self, name: str) -> InputStream[Any]: ...

    def output(self, name: str) -> OutputStream[Any]: ...

    def trace(self, event: str, **attrs: Any) -> None:
        """Record a trace event. Never pass raw audio; text only where redaction allows."""
        ...

    async def call_host(
        self,
        op: str,
        args: Mapping[str, Any],
        *,
        audio_in: AsyncIterable[bytes] | None = None,
        audio_format: AudioFormat | None = None,
    ) -> Any:
        """Ask the turn's client to perform ``op`` (declared in ``spec.host_ops``).

        With ``audio_in`` the PCM chunks (in ``audio_format``) are streamed to the host while the
        operation runs (e.g. speech-to-text). Raises ``HostUnavailable`` if the client does not
        offer the operation, ``HostError`` if it failed.
        """
        ...

    def stream_host(
        self, op: str, args: Mapping[str, Any]
    ) -> AbstractAsyncContextManager[HostAudio]:
        """Perform ``op`` and stream back the audio it produces (e.g. text-to-speech)."""
        ...


class Node(Protocol):
    """A node type implementation. One instance per node in a deployed graph."""

    spec: ClassVar[NodeSpec]

    async def start(self, ctx: NodeContext) -> None:
        """Instance lifetime begins: validate config, load models, open pools."""
        ...

    async def run_turn(self, turn: TurnSession) -> None:
        """Handle one turn: read inputs, write outputs."""
        ...

    async def stop(self) -> None:
        """Instance lifetime ends: release resources."""
        ...
