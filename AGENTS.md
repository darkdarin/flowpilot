# AGENTS.md

**FlowPilot** is a visual orchestrator for voice and AI flows, built for Home Assistant users, with a
core that does not depend on Home Assistant. A graph of typed nodes (STT, speaker ID, policy, agents,
TTS) replaces the fixed Assist pipeline; providers and devices are plugins.

## Language

- Everything inside this repository is in **English**: code, identifiers, comments, docstrings,
  README/CONTRIBUTING, this file, commit messages.
- Research, planning, architecture decisions (ADRs) and the roadmap live in **Notion, in Russian**.

## Documentation (read before working)

Development documentation is kept in **Notion**, page
[FlowPilot](https://app.notion.com/p/40de74ee720f8085816fccb77ca103f7); read and edit it via the Notion MCP.

- [01 · Concept, goals and requirements](https://app.notion.com/p/40de74ee720f81a3902bdd50f9dde41c)
- [02 · Architecture and runtime](https://app.notion.com/p/40de74ee720f8181975ce70e9ee8b6b3)
- [03 · Node catalog and contracts](https://app.notion.com/p/40de74ee720f81a5b404d5b0cf65d433) —
  contracts v1 (ADR-013); the code in `packages/flowpilot-sdk` is the source of truth for signatures
- [04 · Integrations: HA, Yandex Station, Soniox, Wyoming](https://app.notion.com/p/40de74ee720f8139b4afff9b2b9d55f8)
- [05 · Speaker identification, security and child mode](https://app.notion.com/p/40de74ee720f81869845d32daf4a8625)
- [07 · Roadmap, backlog and architecture decisions (ADR)](https://app.notion.com/p/40de74ee720f81fdad0ac07cab8fe6c8)
- [08 · Development: environment and commands](https://app.notion.com/p/40de74ee720f8129b245de50c6bfa7e1)
- [Поставка в Home Assistant](https://app.notion.com/p/410e74ee720f81f184cfc6ecf07080ba) — how the
  family of projects ships: README, branding, app image, changelog, releases, CI (shared practice)

Page "06 · Prototype v0.1" describes a starter archive that is not in this repository; reference only.

## Components

| Path | What |
|---|---|
| `packages/flowpilot-sdk/` | Plugin SDK: payload types, `Envelope`, stream and node interfaces |
| `packages/flowpilot-core/` | Runtime: graph validation and execution |
| `plugins/` | Node plugins |
| `custom_components/flowpilot/` | Home Assistant companion integration (Assist mode, ADR-011/012); repo root for HACS/Marketplace |
| `ha-integration/` | Test environment for the integration: own uv project pinned to one HA release |
| `addon/` | Home Assistant app (add-on); image `ghcr.io/darkdarin/flowpilot` published by `release.yml` |
| `editor/` | Visual graph editor (Vue 3 + Vue Flow) |

## Environment

- Python 3.14, `uv` workspace. Checks: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest` (the same steps run in CI).
- Develop against the **local Home Assistant** in `~/Projects/ha-dev` first (HA core from source, same
  versions as the owner's production HA; see Notion "08 · Development"). The owner's live HA (RPi 5) is
  only for final checks: restarting it or changing devices affects a real home, so do it deliberately and
  say so.
- The reference scenario uses the owner's sibling repositories: `~/Projects/ha-yandex-station`
  (Yandex Station integration) and `~/Projects/ha-wyoming-soniox` (Soniox Wyoming server).

## Repository

- GitHub: `darkdarin/flowpilot`, default branch `main`, push over SSH.
- The global `gh` account is a work account; do not run `gh auth switch`. Use the personal account per
  command: `GH_TOKEN="$(gh auth token --user darkdarin)" gh ...` and never print the token.
- Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`); the git author is
  configured locally in this repository.
- License: Apache-2.0 (ADR-010), no CLA.
- Releases: bump the version everywhere (`addon/config.yaml`, `custom_components/flowpilot/manifest.json`,
  every `pyproject.toml` and `__version__` under `packages/` and `plugins/`, then `uv lock` in the root
  and in `ha-integration/`), add a `## <version>` section to `addon/CHANGELOG.md`, push the tag
  `v<version>`. `release.yml` checks the versions, publishes `ghcr.io/darkdarin/flowpilot:<version>`
  (amd64 + arm64) and creates the GitHub release; users get the app update and the integration
  (Marketplace/HACS) from it. A release is public: do it when the owner asks.
- A version section of the changelog has `### App` and `### Integration`; a part that did not change
  says "No changes, no need to update." Home Assistant shows this text for the app update and the
  Marketplace/HACS for the integration update: users see two updates with one version and must know
  which one matters. The app is called **FlowPilot**, the integration **FlowPilot Assist**.

## Rules

- Record architecture decisions as ADRs in Notion (page 07), including trade-offs.
- The core and the SDK never import Home Assistant code (ADR-008); everything HA-specific lives in
  `custom_components/` and adapters.
- Do not rely on private Home Assistant internals without a regression test; monkeypatching Assist is
  forbidden (ADR-011).
- Do not mark an integration as done without a real end-to-end test.
- Never commit secrets, tokens or voice recordings; never read `.env`/`.envrc` files with secrets.
