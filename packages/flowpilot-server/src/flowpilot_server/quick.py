"""Home Assistant's quick commands: the built-in agent first, an LLM only for the rest.

Home Assistant's own conversation agent matches sentences ("turn on the kitchen light") in tens of
milliseconds and runs them; an LLM takes seconds. This node hands the utterance to it: what it
understood and did comes out as the answer (and its speech), what it did not passes on unchanged
on ``unhandled``, to an agent. Join the two answers with First present in front of the client
output and text-to-speech. The exchange goes into the session's dialogue, so the agent knows
what was done ("turn it back on").
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from flowpilot_sdk import (
    LOG_EVENT,
    OPTIONS_KEY,
    HostError,
    HostUnavailable,
    NodeContext,
    NodeSpec,
    PortSpec,
    Response,
    TextDelta,
    TurnSession,
    Utterance,
    dialogue,
    node_translations,
)

from .nodes import HA_CONVERSATION_OP

TYPE = "ha.quick"
BUILT_IN_AGENT = "conversation.home_assistant"
UTTERANCE = "text.utterance/1"


class HaQuickCommands:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Home Assistant quick commands",
        category="agent",
        inputs={
            "utterance": PortSpec(
                UTTERANCE, kind="value", description="What the user said or typed."
            ),
        },
        outputs={
            "response": PortSpec(
                "agent.response/1",
                kind="value",
                description="Home Assistant's answer, when it understood and did the command.",
            ),
            "delta": PortSpec(
                "text.delta/1", description="The same answer as text for speech synthesis."
            ),
            "unhandled": PortSpec(
                UTTERANCE,
                kind="value",
                description="The utterance, unchanged, when Home Assistant did not handle it.",
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "title": "Agent",
                    "description": (
                        "The conversation agent that matches sentences; Home Assistant's own "
                        "when not set. Your own sentences (custom_sentences) work with it too."
                    ),
                    "type": "string",
                    "minLength": 1,
                    OPTIONS_KEY: "ha.conversation",
                },
            },
            "additionalProperties": False,
        },
        host_ops=frozenset({HA_CONVERSATION_OP}),
        turn_timeout_s=20.0,
        description=(
            "Runs commands Home Assistant understands by itself (turn on the light, set the "
            "temperature, start a timer) in a fraction of a second, without an LLM; everything "
            "else goes on unchanged to the next agent. Join their answers with First present."
        ),
    )

    def __init__(self) -> None:
        self._agent = BUILT_IN_AGENT
        self._node = ""

    async def start(self, ctx: NodeContext) -> None:
        self._agent = str(ctx.config.get("agent_id") or BUILT_IN_AGENT)
        self._node = ctx.node_id

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        said: Utterance | None = await turn.input("utterance").value()
        if said is None or not said.text.strip():
            return
        ctx = turn.context
        args: dict[str, Any] = {
            "text": said.text,
            "language": said.language or ctx.language,
            "agent_id": self._agent,
            "conversation_id": ctx.external.get("ha.conversation_id"),
            "device_id": ctx.device.device_id if ctx.device else None,
            "satellite_id": ctx.device.satellite_id if ctx.device else None,
        }
        started = time.monotonic()
        try:
            result = await turn.call_host(HA_CONVERSATION_OP, args)
        except (HostError, HostUnavailable) as err:
            # no Home Assistant to ask: the agent after this node answers instead
            result = {"response_type": "error", "error_code": "unavailable", "speech": str(err)}
        ms = round(1000 * (time.monotonic() - started))
        kind = str(result.get("response_type") or "")
        code = result.get("error_code")
        speech = str(result.get("speech") or "").strip()
        handled = kind != "error" and bool(speech or kind == "action_done")
        turn.trace("ha.quick", handled=handled, response=kind, code=code, ms=ms)
        if getattr(turn, "watched", False):
            summary = (
                f"done by Home Assistant ({kind}) in {ms} ms"
                if handled
                else f"passed on ({code or kind or 'no answer'}) after {ms} ms"
            )
            turn.trace(
                LOG_EVENT, kind="quick", summary=summary, detail=speech or None, ms=ms, ok=handled
            )
        if not handled:
            await turn.output("unhandled").send(said)
            return
        language = args["language"]
        await dialogue.add(turn.shared, said.text, speech, by=self._node)
        if speech:
            await turn.output("delta").send(TextDelta(speech))
        await turn.output("response").send(
            Response(
                text=speech,
                language=language,
                continue_conversation=bool(result.get("continue_conversation", False)),
            )
        )
