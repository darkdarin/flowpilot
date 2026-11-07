"""Node interface: static spec, instance lifecycle and the per-turn session."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from .commands import QuickCommands
from .context import SessionInfo, SessionState, TurnContext
from .host import HostAudio
from .memory import Memory
from .payloads import AudioFormat
from .streams import InputStream, OutputStream
from .text import LANGUAGE_RE, NodeText
from .types import PRIVILEGED_TYPES, union, union_members, validate_type_id

# Any text: what speech-to-text heard, an utterance, a text stream, an agent's answer. A port of
# this type takes whichever is connected; with ``var`` the node passes the same type on.
TEXT = union("text.transcript/1", "text.utterance/1", "text.delta/1", "agent.response/1")
# Any text or audio: for nodes that join branches whatever they carry.
TEXT_OR_AUDIO = union(*TEXT.split("|"), "audio.pcm/1")

NODE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

Category = Literal["input", "speech", "identity", "logic", "agent", "tool", "output", "debug"]
Isolation = Literal["in_process", "subprocess", "any"]


@dataclass(frozen=True, slots=True)
class PortSpec:
    """A typed port of a node.

    ``type`` is one payload type id, or a union of them (``"a/1|b/1"``, e.g. ``TEXT``): such a
    port takes any of them, and its kind is that of the type connected (``kind`` applies to
    one-type ports). ``var`` makes the node generic: its ports with the same ``var`` carry the
    same type in a graph, the one connected to its inputs; an output with ``var`` has the type
    of the inputs with it.
    """

    type: str
    kind: Literal["stream", "value"] = "stream"
    required: bool = True
    delivery: Literal["lossless", "latest"] = "lossless"
    buffer: int = 32
    description: str = ""  # English; translations in NodeSpec.translations
    var: str | None = None

    def __post_init__(self) -> None:
        members = union_members(self.type)
        for member in members:
            validate_type_id(member)
        if len(set(members)) != len(members):
            raise ValueError(f"port type {self.type!r} repeats a type")
        if self.var is not None and not self.var.isidentifier():
            raise ValueError(f"invalid type variable {self.var!r}")
        if self.buffer < 1:
            raise ValueError("port buffer must be at least 1")

    @property
    def types(self) -> tuple[str, ...]:
        """The payload types this port takes or gives."""
        return union_members(self.type)

    @property
    def is_union(self) -> bool:
        return len(self.types) > 1


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
# Secret references in ``config`` arrive resolved to their values (a cloud API's key), as
# ``NodeContext.secret`` would give them at start; unset secrets stay references.
# Language settings carry ``"format": "language"`` so editors show language names for the codes.
NODE_OPTIONS_KEY = "x-flowpilot-node-options"

# JSON Schema keyword with English labels for the values of an ``enum`` setting:
# ``{"enum": ["whole", "words"], ENUM_TITLES_KEY: {"whole": "At once", "words": "Word by word"}}``.
# Translations carry the same under ``config.<setting>.options``.
ENUM_TITLES_KEY = "x-flowpilot-enum-titles"

# Settings that belong together: the config schema names its groups with their English texts,
# ``{GROUPS_KEY: {"memory": {"title": "Memory", "description": "..."}}}``, and each grouped
# property says ``{GROUP_KEY: "memory"}``. Editors show a group as one entry that opens its
# settings; translations carry the texts under ``groups.<group>``.
GROUPS_KEY = "x-flowpilot-groups"
GROUP_KEY = "x-flowpilot-group"


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
    host_ops: frozenset[str] = frozenset()  # host operations the node needs (ADR-014)
    # Host operations the node uses when the turn's client offers them and works without (e.g.
    # who lives in the home, for an agent that also runs without Home Assistant): they are not
    # required of clients, and ``call_host`` raises ``HostUnavailable`` when they are missing.
    optional_host_ops: frozenset[str] = frozenset()
    description: str = ""
    # A debugging tap (records or shows what reaches it): it only runs while someone watches the
    # graph in live debug; otherwise its inputs get nothing and the turn does not wait for it.
    # Taps have no outputs, so nothing else depends on them.
    debug_tap: bool = False
    # A boundary node every graph has exactly once (the client input and output of a turn): it is
    # created with the graph, and editors neither offer it nor let it be deleted.
    fixed: bool = False
    # Texts in other languages (see flowpilot_sdk.text); English is title/description above.
    translations: Mapping[str, NodeText] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for op in self.host_ops | self.optional_host_ops:
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
            for member in port.types:
                grant = PRIVILEGED_TYPES.get(member)
                if grant is not None and grant not in self.grants:
                    raise ValueError(
                        f"{self.type}: output {name!r} of privileged type {member!r} "
                        f"requires grant {grant!r}"
                    )
        inputs_vars = {p.var for p in self.inputs.values() if p.var}
        for name, port in self.outputs.items():
            if port.var and port.var not in inputs_vars:
                raise ValueError(f"{self.type}: output {name!r} uses {port.var!r}, no input does")
        if self.turn_timeout_s is not None and self.turn_timeout_s <= 0:
            raise ValueError("turn_timeout_s must be positive")
        if self.debug_tap and (self.outputs or self.category != "debug"):
            raise ValueError(f"{self.type}: a debug tap has no outputs and the debug category")
        settings = set(self.config_schema.get("properties") or {})
        groups = set(self.config_schema.get(GROUPS_KEY) or {})
        for name, prop in (self.config_schema.get("properties") or {}).items():
            group = prop.get(GROUP_KEY)
            if group is not None and group not in groups:
                raise ValueError(f"{self.type}: setting {name!r} is in unknown group {group!r}")
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
            if unknown := set(text.groups) - groups:
                raise ValueError(f"{where}: translations for unknown groups {sorted(unknown)}")
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
    # The runtime's long-term memory (facts about the home and its people); ``None`` when the
    # runtime keeps none. Read it per turn: people edit it while graphs run.
    memory: Memory | None
    # Quick commands people taught the assistant (flowpilot_sdk.commands); ``None`` when the
    # runtime keeps none. Read them per turn: people add and remove them while graphs run.
    commands: QuickCommands | None

    def secret(self, name: str) -> str:
        """Value of a secret referenced in the node config; ``KeyError`` if not granted."""
        ...


# Trace event of a node's journal, shown in live debug as a timeline of what the node did:
# ``turn.trace(LOG_EVENT, kind="llm.request", summary="...", detail="...", **numbers)``. ``kind``
# names the entry (editors may show known kinds specially), ``summary`` is one short line,
# ``detail`` the content (prompts, arguments, results). Content only when ``turn.watched``.
LOG_EVENT = "log"


class TurnSession(Protocol):
    """What a node sees during one turn."""

    context: TurnContext
    session: SessionInfo
    state: SessionState
    # State every node of the session shares (the dialogue, ``flowpilot_sdk.dialogue``); a
    # node's own state is ``state``.
    shared: SessionState
    # Someone watches the graph in live debug: the node may put content (texts, arguments,
    # prompts) into its trace events and journal (``LOG_EVENT``); otherwise it must not.
    watched: bool

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
        """Ask the turn's client to perform ``op`` (declared in ``spec.host_ops`` or
        ``spec.optional_host_ops``).

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
