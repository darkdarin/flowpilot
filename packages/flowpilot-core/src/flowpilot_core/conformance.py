"""Plugin conformance kit: checks every node implementation must pass.

Framework-agnostic: :func:`check_node` returns a :class:`ConformanceReport`;
:func:`assert_conforms` turns violations into an ``AssertionError`` for pytest.

Checks (codes are stable):

- ``C1`` config schema is valid JSON Schema and the sample config conforms to it
- ``C2`` ``start``/``stop`` succeed and leave no running tasks behind
- ``C3`` all inputs absent (e.g. a text-only turn): finishes in time without raising
- ``C4`` all inputs failed: finishes in time (raising is allowed, reported as a warning)
- ``C5`` sample inputs: finishes in time without raising (payload types and single-value
  outputs are enforced by the runtime streams and surface here as errors)
- ``C6`` cancellation: stops within the grace period and does not swallow ``CancelledError``
- ``C7`` no tasks started by the node are left running after ``run_turn``
- ``W1`` (warning) speech/identity/agent/tool nodes should declare ``turn_timeout_s``
- ``W2`` (warning) the node, its ports and settings should describe themselves (English
  ``description``; the editor shows it, translations fall back to it)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from flowpilot_sdk import (
    Envelope,
    Node,
    NodeSpec,
    Principal,
    SessionInfo,
    SourceInfo,
    StreamHeader,
    TurnContext,
    new_ulid,
)

from .runtime.channel import Channel, End, Fail, Item, Message, Open
from .runtime.deployment import HostCaller, RuntimeNodeContext, RuntimeTurnSession
from .runtime.state import MemorySessionStore
from .runtime.streams import RuntimeInputStream, RuntimeOutputStream
from .runtime.trace import ListTraceSink
from .validate import collect_secret_refs, secret_aware_schema

DEFAULT_BUDGET_S = 5.0
DEFAULT_GRACE_S = 0.5
TIMEOUT_EXPECTED = frozenset({"speech", "identity", "agent", "tool"})

Terminal = Literal["end", "fail", "hang", "absent"]
Prepare = Callable[[Node, TurnContext], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class InputFeed:
    """What a test input port receives: optional header meta, items, then a terminal state."""

    items: Sequence[Any] = ()
    meta: Mapping[str, Any] | None = None
    end: Terminal = "end"
    delay_s: float = 0.0


@dataclass(slots=True)
class CapturedOutput:
    header: StreamHeader | None = None
    items: list[Any] = field(default_factory=list)
    terminal: Literal["end", "fail", "none"] = "none"
    fail_reason: str | None = None


@dataclass(slots=True)
class NodeRun:
    outputs: dict[str, CapturedOutput]
    error: BaseException | None
    duration_s: float
    leaked_tasks: list[str]
    trace: ListTraceSink


@dataclass(slots=True)
class ConformanceReport:
    node_type: str
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def __str__(self) -> str:
        lines = [f"{self.node_type}: {'OK' if self.ok else 'FAILED'}"]
        lines += [f"  violation {v}" for v in self.violations]
        lines += [f"  warning   {w}" for w in self.warnings]
        return "\n".join(lines)


def _turn_context(deadline_s: float) -> tuple[TurnContext, SessionInfo]:
    now = datetime.now(UTC)
    session = SessionInfo(session_id=new_ulid(), created_at=now, turn_index=0)
    ctx = TurnContext(
        turn_id=new_ulid(),
        session_id=session.session_id,
        started_at=now,
        deadline_mono_ns=time.monotonic_ns() + int(deadline_s * 1e9),
        source=SourceInfo(kind="conformance", adapter_id="conformance"),
        trace_id=new_ulid(),
        initial_principal=Principal.unknown(),
    )
    return ctx, session


async def _feed(channel: Channel, type_id: str, feed: InputFeed, turn_id: str) -> None:
    if feed.end == "absent":
        await channel.put(End())
        return
    if feed.end == "fail" and not feed.items:
        await channel.put(Fail("conformance: upstream failed"))
        return
    await channel.put(Open(StreamHeader(type_id, dict(feed.meta or {}))))
    for seq, payload in enumerate(feed.items):
        if feed.delay_s:
            await asyncio.sleep(feed.delay_s)
        await channel.put(Item(Envelope(turn_id, seq, time.monotonic_ns(), payload)))
    if feed.end == "end":
        await channel.put(End())
    elif feed.end == "fail":
        await channel.put(Fail("conformance: upstream failed"))
    else:  # hang: stay open until cancelled
        await asyncio.Event().wait()


async def _drain(channel: Channel, captured: CapturedOutput) -> None:
    while True:
        msg: Message = await channel.get()
        if isinstance(msg, Open):
            captured.header = msg.header
        elif isinstance(msg, Item):
            captured.items.append(msg.envelope.payload)
        elif isinstance(msg, Fail):
            captured.terminal, captured.fail_reason = "fail", msg.reason
            return
        else:
            captured.terminal = "end"
            return


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0.01)


def _new_tasks(before: set[asyncio.Task[Any]]) -> list[str]:
    current = asyncio.current_task()
    return sorted(
        t.get_name() for t in asyncio.all_tasks() - before if t is not current and not t.done()
    )


async def run_node_once(
    node: Node,
    inputs: Mapping[str, InputFeed],
    *,
    budget_s: float = DEFAULT_BUDGET_S,
    prepare: Prepare | None = None,
    cancel_after_s: float | None = None,
    host: HostCaller | None = None,
) -> NodeRun:
    """Run one turn of a started node against fed inputs, capturing outputs.

    Inputs not listed in ``inputs`` are absent. With ``cancel_after_s`` the turn is cancelled after
    that delay and ``error`` holds the resulting ``CancelledError`` if the node propagated it.
    """
    spec = node.spec
    ctx, session = _turn_context(budget_s)
    if prepare is not None:
        prepared = prepare(node, ctx)
        if prepared is not None:
            await prepared

    before = set(asyncio.all_tasks())
    feeders: list[asyncio.Task[None]] = []
    drains: list[asyncio.Task[None]] = []
    in_streams: dict[str, RuntimeInputStream] = {}
    for name, port in spec.inputs.items():
        channel = Channel(port.buffer, latest=port.delivery == "latest")
        in_streams[name] = RuntimeInputStream(name, port, channel)
        feed = inputs.get(name, InputFeed(end="absent"))
        feeders.append(asyncio.create_task(_feed(channel, port.type, feed, ctx.turn_id)))

    captured: dict[str, CapturedOutput] = {}
    out_streams: dict[str, RuntimeOutputStream] = {}
    for name, port in spec.outputs.items():
        channel = Channel(1024)
        captured[name] = CapturedOutput()
        out_streams[name] = RuntimeOutputStream(spec.type, name, port, [channel], ctx.turn_id)
        drains.append(asyncio.create_task(_drain(channel, captured[name])))

    trace = ListTraceSink()
    turn = RuntimeTurnSession(
        spec.type,
        ctx,
        session,
        MemorySessionStore().view(session.session_id, spec.type),
        in_streams,
        out_streams,
        trace,
        host=host,
        host_ops=spec.host_ops,
    )

    started = time.monotonic()
    error: BaseException | None = None
    task = asyncio.create_task(node.run_turn(turn), name=f"conformance:{spec.type}")
    try:
        if cancel_after_s is not None:
            await asyncio.wait({task}, timeout=cancel_after_s)
            if not task.done():
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=budget_s)
        else:
            done, _ = await asyncio.wait({task}, timeout=budget_s)
        if task in done:
            try:
                task.result()
            except BaseException as err:  # noqa: BLE001 - we report whatever the node raised
                error = err
        else:
            error = TimeoutError(f"run_turn did not finish within {budget_s}s")
            task.cancel()
    finally:
        duration = time.monotonic() - started
        for out in out_streams.values():
            if error is None:
                await out.end()
            else:
                await out.fail(type(error).__name__)
        for inp in in_streams.values():
            await inp.aclose()
        # Outputs are terminated above, so drains finish on their own; feeders may hang.
        if drains:
            await asyncio.wait(drains, timeout=1.0)
        for helper in (*feeders, *drains):
            helper.cancel()
        await asyncio.gather(*feeders, *drains, return_exceptions=True)

    await _settle()
    leaked = [n for n in _new_tasks(before) if n != task.get_name()]
    return NodeRun(captured, error, duration, leaked, trace)


def missing_descriptions(spec: NodeSpec) -> list[str]:
    """What has no English description: ``node``, ``input x``, ``output y``, ``setting z``."""
    missing = [] if spec.description else ["node"]
    missing += [f"input {name}" for name, port in spec.inputs.items() if not port.description]
    missing += [f"output {name}" for name, port in spec.outputs.items() if not port.description]
    props = spec.config_schema.get("properties") or {}
    missing += [
        f"setting {name}"
        for name, prop in props.items()
        if not (isinstance(prop, Mapping) and prop.get("description"))
    ]
    return missing


async def check_node(
    node_cls: type[Node],
    *,
    config: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    sample: Mapping[str, InputFeed] | None = None,
    prepare: Prepare | None = None,
    grace_s: float = DEFAULT_GRACE_S,
    host: HostCaller | None = None,
) -> ConformanceReport:
    """Run all conformance checks for ``node_cls``."""
    spec = node_cls.spec
    config = dict(config or {})
    report = ConformanceReport(spec.type)
    budget = spec.turn_timeout_s or DEFAULT_BUDGET_S

    def violation(code: str, message: str) -> None:
        report.violations.append(f"{code} {message}")

    def warning(code: str, message: str) -> None:
        report.warnings.append(f"{code} {message}")

    # C1: config schema
    try:
        schema = secret_aware_schema(spec.config_schema)
        Draft202012Validator.check_schema(schema)
        for err in Draft202012Validator(schema).iter_errors(config):
            violation("C1", f"sample config does not match config_schema: {err.message}")
    except SchemaError as err:
        violation("C1", f"config_schema is not valid JSON Schema: {err.message}")

    if spec.category in TIMEOUT_EXPECTED and spec.turn_timeout_s is None:
        warning("W1", f"{spec.category} node declares no turn_timeout_s")

    if missing := missing_descriptions(spec):
        warning("W2", f"no description for {', '.join(missing)}")

    refs: set[str] = set()
    collect_secret_refs(config, refs)
    ctx = RuntimeNodeContext(spec.type, config, dict(secrets or {}), frozenset(refs))

    # C2: lifecycle
    before = set(asyncio.all_tasks())
    node = node_cls()
    try:
        await node.start(ctx)
    except Exception as err:  # noqa: BLE001 - reported as a violation
        violation("C2", f"start() raised {type(err).__name__}: {err}")
        return report

    try:
        absent_inputs = {name: InputFeed(end="absent") for name in spec.inputs}
        run = await run_node_once(node, absent_inputs, budget_s=budget, prepare=prepare, host=host)
        _check_run(report, "C3", "absent inputs", run, raising_is_violation=True)

        failed = {name: InputFeed(end="fail") for name in spec.inputs}
        run = await run_node_once(node, failed, budget_s=budget, prepare=prepare, host=host)
        _check_run(report, "C4", "failed inputs", run, raising_is_violation=False)

        if sample is not None:
            run = await run_node_once(node, sample, budget_s=budget, prepare=prepare, host=host)
            _check_run(report, "C5", "sample inputs", run, raising_is_violation=True)
            if run.error is None:
                for name, out in run.outputs.items():
                    if out.terminal == "end" and out.header is None and not out.items:
                        continue  # absent output is fine
                    if out.terminal != "end":
                        violation("C5", f"output {name!r} did not end cleanly ({out.terminal})")

        hanging = {name: (sample or {}).get(name, InputFeed()) for name in spec.inputs}
        hanging = {
            name: InputFeed(items=f.items, meta=f.meta, end="hang", delay_s=f.delay_s)
            for name, f in hanging.items()
        }
        run = await run_node_once(
            node, hanging, budget_s=grace_s, prepare=prepare, cancel_after_s=0.05, host=host
        )
        if isinstance(run.error, TimeoutError):
            violation("C6", f"did not stop within {grace_s}s after cancellation")
        elif run.error is None and run.duration_s >= 0.05:
            violation("C6", "swallowed CancelledError (returned normally after cancel)")
        elif run.error is not None and not isinstance(run.error, asyncio.CancelledError):
            violation("C6", f"raised {type(run.error).__name__} instead of CancelledError")
        if run.leaked_tasks:
            violation("C7", f"tasks left running after cancellation: {run.leaked_tasks}")
    finally:
        try:
            await node.stop()
        except Exception as err:  # noqa: BLE001 - reported as a violation
            violation("C2", f"stop() raised {type(err).__name__}: {err}")
        await _settle()
        leaked = _new_tasks(before)
        if leaked:
            violation("C2", f"tasks left running after stop(): {leaked}")
    return report


def _check_run(
    report: ConformanceReport, code: str, label: str, run: NodeRun, *, raising_is_violation: bool
) -> None:
    if isinstance(run.error, TimeoutError):
        report.violations.append(f"{code} {label}: did not finish within the turn budget")
    elif run.error is not None:
        message = f"{code} {label}: raised {type(run.error).__name__}: {run.error}"
        (report.violations if raising_is_violation else report.warnings).append(message)
    if run.leaked_tasks:
        report.violations.append(f"C7 {label}: tasks left running: {run.leaked_tasks}")


def assert_conforms(report: ConformanceReport) -> None:
    """Raise ``AssertionError`` with every violation; warnings do not fail."""
    if not report.ok:
        raise AssertionError(str(report))
