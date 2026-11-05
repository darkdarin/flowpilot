"""Render every FlowPilot brand image from one definition of the mark.

The mark: a node (teal) with two typed wires, one ending in a diamond (a value port, amber), the
other in a circle (a stream port, violet), the way the editor draws ports and wires.

    uv run --no-project --with pillow python scripts/brand.py --font Onest.ttf

``--font`` is the Onest variable TTF (https://github.com/google/fonts/tree/main/ofl/onest), used
for the wordmark in the logos. Writes:

- ``assets/brand/flowpilot-mark.svg``       vector source of the mark
- ``custom_components/flowpilot/brand/``    icon, logo (+ @2x, dark_logo) for Home Assistant
- ``addon/icon.png``, ``addon/logo.png``    the app in the app store
- ``assets/brand/social-preview.png``       GitHub social preview (upload by hand)

Brand images follow home-assistant/brands: PNG, transparent, no empty margins.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

TEAL = "#2BB8A6"
VIOLET = "#9C7BF0"
AMBER = "#E9A23B"
INK_LIGHT = "#1D2330"  # wordmark on light backgrounds
INK_DARK = "#E4E7EC"  # wordmark on dark backgrounds
INK_ANY = "#6B7484"  # wordmark that reads on both (the app store has no dark variant)
CANVAS = "#1B1F26"

# Geometry in a unit square (the same numbers go into the SVG)
NODE = (0.24, 0.5, 0.13)  # cx, cy, r
WIRE = 0.075
WIRES = [  # start, control 1, control 2, end, color
    ((0.24, 0.5), (0.52, 0.5), (0.5, 0.27), (0.78, 0.27), TEAL),
    ((0.24, 0.5), (0.52, 0.5), (0.5, 0.73), (0.78, 0.73), VIOLET),
]
DIAMOND = (0.80, 0.27, 0.095 * 1.25)  # cx, cy, half diagonal
CIRCLE = (0.80, 0.73, 0.095)


def _bezier(p0, p1, p2, p3, n=400):  # type: ignore[no-untyped-def]
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        yield (
            u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
        )


def mark(size: int) -> Image.Image:
    """The mark, trimmed to its content and centred in a transparent square of ``size``."""
    s = 2048
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = WIRE * s
    for p0, p1, p2, p3, color in WIRES:
        for x, y in _bezier(p0, p1, p2, p3):
            d.ellipse([x * s - w / 2, y * s - w / 2, x * s + w / 2, y * s + w / 2], fill=color)
    cx, cy, r = (v * s for v in NODE)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TEAL)
    cx, cy, h = (v * s for v in DIAMOND)
    d.polygon([(cx, cy - h), (cx + h, cy), (cx, cy + h), (cx - h, cy)], fill=AMBER)
    cx, cy, r = (v * s for v in CIRCLE)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=VIOLET)
    trimmed = img.crop(img.getbbox())
    side = max(trimmed.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(trimmed, ((side - trimmed.width) // 2, (side - trimmed.height) // 2))
    return square.resize((size, size), Image.Resampling.LANCZOS)


def mark_svg() -> str:
    """The same mark as SVG, viewBox trimmed to the content (units of 1/1000)."""
    k = 1000

    def pt(p: tuple[float, float]) -> str:
        return f"{p[0] * k:g} {p[1] * k:g}"

    nx, ny, nr = NODE
    dx, dy, dh = DIAMOND
    cx, cy, cr = CIRCLE
    left, right = (nx - nr) * k, (dx + dh) * k
    top, bottom = (dy - dh) * k, (cy + cr) * k
    paths = "\n".join(
        f'  <path d="M{pt(p0)} C{pt(p1)} {pt(p2)} {pt(p3)}" stroke="{color}" />'
        for p0, p1, p2, p3, color in WIRES
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="{left:g} {top:g} {right - left:g} {bottom - top:g}">
<g fill="none" stroke-width="{WIRE * k:g}" stroke-linecap="round">
{paths}
</g>
<circle cx="{nx * k:g}" cy="{ny * k:g}" r="{nr * k:g}" fill="{TEAL}" />
<path d="M{dx * k:g} {(dy - dh) * k:g} L{(dx + dh) * k:g} {dy * k:g} L{dx * k:g} {(dy + dh) * k:g} L{(dx - dh) * k:g} {dy * k:g} Z" fill="{AMBER}" />
<circle cx="{cx * k:g}" cy="{cy * k:g}" r="{cr * k:g}" fill="{VIOLET}" />
</svg>
"""  # noqa: E501


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(path), size)
    f.set_variation_by_axes([640])
    return f


def logo(height: int, ink: str, font_path: Path) -> Image.Image:
    """Mark and wordmark side by side, trimmed: the short side is ``height``."""
    scale = 4
    h = height * scale
    m = mark(h)
    f = font(font_path, int(h * 0.62))
    text = "FlowPilot"
    box = f.getbbox(text)
    gap = int(h * 0.22)
    canvas = Image.new("RGBA", (h + gap + box[2] + h, h), (0, 0, 0, 0))
    canvas.paste(m, (0, 0), m)
    d = ImageDraw.Draw(canvas)
    d.text((h + gap, (h - (box[3] + box[1])) // 2), text, font=f, fill=ink)
    trimmed = canvas.crop(canvas.getbbox())
    width = round(trimmed.width * height / trimmed.height)
    return trimmed.resize((width, height), Image.Resampling.LANCZOS)


def fit(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale ``img`` into a ``width``x``height`` transparent box (the app logo has a fixed box)."""
    img = img.copy()
    img.thumbnail((width, height), Image.Resampling.LANCZOS)
    box = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    box.paste(img, ((width - img.width) // 2, (height - img.height) // 2), img)
    return box


def social(font_path: Path) -> Image.Image:
    img = Image.new("RGB", (1280, 640), CANVAS)
    m = mark(300)
    img.paste(m, (120, 170), m)
    d = ImageDraw.Draw(img)
    d.text((480, 205), "FlowPilot", font=font(font_path, 116), fill=INK_DARK)
    tagline = font(font_path, 40)
    tagline.set_variation_by_axes([420])
    d.text((484, 360), "Voice and AI flows for Home Assistant,", font=tagline, fill="#8A93A3")
    d.text((484, 410), "built as a graph", font=tagline, fill="#8A93A3")
    return img


def save(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)
    print(f"{path.relative_to(ROOT)}  {img.width}x{img.height}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, required=True, help="Onest variable TTF")
    font_path = parser.parse_args().font

    assets = ROOT / "assets" / "brand"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "flowpilot-mark.svg").write_text(mark_svg())
    print("assets/brand/flowpilot-mark.svg")

    brand = ROOT / "custom_components" / "flowpilot" / "brand"
    save(mark(256), brand / "icon.png")
    save(mark(512), brand / "icon@2x.png")
    save(logo(128, INK_LIGHT, font_path), brand / "logo.png")
    save(logo(256, INK_LIGHT, font_path), brand / "logo@2x.png")
    save(logo(128, INK_DARK, font_path), brand / "dark_logo.png")
    save(logo(256, INK_DARK, font_path), brand / "dark_logo@2x.png")

    save(mark(128), ROOT / "addon" / "icon.png")
    save(fit(logo(200, INK_ANY, font_path), 250, 100), ROOT / "addon" / "logo.png")

    save(social(font_path), assets / "social-preview.png")


if __name__ == "__main__":
    main()
