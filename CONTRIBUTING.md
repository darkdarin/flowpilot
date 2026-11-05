# Contributing to FlowPilot

Thanks for your interest! The project is at an early stage, so please open an issue to discuss a change
before starting significant work.

## Setup

```sh
uv sync
```

## Checks

All of these run in CI and must pass:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Conventions

- Code, identifiers, comments and repository docs are in English.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat: ...`,
  `fix: ...`, `docs: ...`, `chore: ...`.
- Every node plugin declares typed ports, a documented timeout and failure behaviour, and passes the
  plugin conformance kit:

  ```python
  from flowpilot_core.conformance import InputFeed, assert_conforms, check_node


  async def test_my_node_conforms() -> None:
      report = await check_node(
          MyNode,
          config={"uri": "tcp://localhost:10300"},
          sample={"audio": InputFeed(items=[...], meta=AudioFormat(16000).as_meta())},
      )
      assert_conforms(report)
  ```

  The kit checks the config schema, start/stop, absent and failed inputs, the sample run,
  cancellation and leaked tasks (see the module docstring for the list of checks).
- Nodes describe themselves for the editor: `NodeSpec.description`, `PortSpec.description` for
  every port, and `title` + `description` for every config schema property, all in English
  (conformance warning `W2` lists what is missing). Other languages go to
  `<package>/translations/<language>.json` (format in `flowpilot_sdk/text.py`) and are loaded with
  `translations=node_translations(__package__, "my.node")`; bundled nodes ship Russian, and
  `packages/flowpilot-server/tests/test_texts.py` keeps them complete.
- A setting that names something a host provides (a Home Assistant entity, a Wyoming service) is
  marked in the config schema, e.g. `{"type": "string", OPTIONS_KEY: "ha.stt"}` with
  `from flowpilot_sdk import OPTIONS_KEY`. The editor then offers the host's list for it, and Home
  Assistant raises a repair issue when a deployed graph refers to something that is gone.
- Do not commit secrets, API keys or voice recordings.

## License of contributions

By submitting a contribution you agree that it is licensed under the
[Apache License 2.0](LICENSE), the same license as the project (inbound = outbound).
A `Signed-off-by` line ([DCO](https://developercertificate.org/)) is welcome but not required.
