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
  plugin conformance tests (once available).
- Do not commit secrets, API keys or voice recordings.

## License of contributions

By submitting a contribution you agree that it is licensed under the
[Apache License 2.0](LICENSE), the same license as the project (inbound = outbound).
A `Signed-off-by` line ([DCO](https://developercertificate.org/)) is welcome but not required.
