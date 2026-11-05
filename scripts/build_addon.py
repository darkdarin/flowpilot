"""Assemble the FlowPilot add-on as a local add-on folder that Home Assistant builds by itself.

    python scripts/build_addon.py            # -> dist/addon/flowpilot/

Copy the resulting ``flowpilot`` folder into ``/addons`` on the Home Assistant machine (Samba or
SSH add-on), then Settings → Add-ons → Add-on Store → ⋮ → Check for updates; "FlowPilot" appears
under "Local add-ons". The Supervisor builds the image on that machine from the bundled sources.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# (the root README stays out: the add-on folder has its own README for the add-on store)
SOURCES = ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "LICENSE", "NOTICE",
           "packages", "plugins", "editor"]  # fmt: skip
IGNORE = shutil.ignore_patterns(
    "node_modules", ".venv", "__pycache__", "*.pyc", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "tests", "*_tests", "editor",  # tests; the built editor is rebuilt in Docker
)  # fmt: skip


def build(out: Path) -> Path:
    target = out / "flowpilot"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(ROOT / "addon", target)
    config = target / "config.yaml"  # without "image:" the Supervisor builds the bundled Dockerfile
    config.write_text("".join(
        line for line in config.read_text().splitlines(keepends=True)
        if not line.startswith("image:")
    ))  # fmt: skip
    for name in SOURCES:
        src = ROOT / name
        if src.is_dir():
            # the editor's own sources must stay; only the build output inside the server is skipped
            ignore = IGNORE if name != "editor" else shutil.ignore_patterns("node_modules", "dist")
            shutil.copytree(src, target / name, ignore=ignore)
        else:
            shutil.copy2(src, target / name)
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "addon")
    target = build(parser.parse_args().out)
    print(f"add-on folder: {target}")


if __name__ == "__main__":
    main()
