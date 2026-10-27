# -*- coding: utf-8 -*-
"""Generate the Nodes Ops favicon / app icon set (pure stdlib, no Pillow).

The mark mirrors the dashboard brand chip: a rounded tile with the blue→green
gradient from ``.brand-mark`` in static/app.css and a white "N".

Usage:
    python deploy/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

# Same stops as .brand-mark: linear-gradient(135deg, #2574eb, #1b62d1, #169c62)
STOPS = (
    (0.00, (0x25, 0x74, 0xEB)),
    (0.50, (0x1B, 0x62, 0xD1)),
    (1.00, (0x16, 0x9C, 0x62)),
)

MARGIN = 0.055      # tile inset from the canvas edge
RADIUS = 0.215      # corner radius, relative to canvas
BAR = 0.22          # N stem width, relative to the glyph box
DIAGONAL_TOP = 0.10  # diagonal start/end insets so it meets both stems


def _lerp(a, b, t):
    return a + (b - a) * t


def _gradient(t):
    t = min(1.0, max(0.0, t))
    for index in range(len(STOPS) - 1):
        start_t, start_c = STOPS[index]
        end_t, end_c = STOPS[index + 1]
        if t <= end_t:
            local = (t - start_t) / (end_t - start_t)
            return tuple(_lerp(start_c[i], end_c[i], local) for i in range(3))
    return STOPS[-1][1]


def _inside_rounded_square(x, y, size):
    """x/y in pixels, tile spans [MARGIN*size, (1-MARGIN)*size]."""
    low = MARGIN * size
    high = (1.0 - MARGIN) * size
    radius = RADIUS * size
    if not (low <= x <= high and low <= y <= high):
        return False
    # Nearest inner corner centre decides whether we are in the corner arc.
    cx = min(max(x, low + radius), high - radius)
    cy = min(max(y, low + radius), high - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _inside_glyph(x, y, size):
    """White "N": two full-height stems plus a slanted bar between them."""
    left = 0.255 * size
    right = 0.745 * size
    width = right - left
    top = 0.255 * size
    bottom = 0.745 * size

    if not (top <= y <= bottom):
        return False

    stem = BAR * width
    if left <= x <= left + stem:
        return True
    if right - stem <= x <= right:
        return True

    # Diagonal band: parameterised along y, swept from the left stem to the right.
    progress = (y - top) / (bottom - top)
    # Shift so the band starts under the left stem's right edge and ends at the right stem.
    centre = left + stem + (width - 2 * stem) * progress
    half = stem / 2.4
    return centre - half <= x <= centre + half


def render_rgba(size, samples=4):
    """Supersampled RGBA scanlines for one square icon."""
    rows = []
    step = 1.0 / samples
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0
            for sy in range(samples):
                for sx in range(samples):
                    x = px + (sx + 0.5) * step
                    y = py + (sy + 0.5) * step
                    if not _inside_rounded_square(x, y, size):
                        continue
                    if _inside_glyph(x, y, size):
                        r += 255
                        g += 255
                        b += 255
                    else:
                        # 135deg diagonal gradient, matching the CSS brand chip.
                        colour = _gradient((x + y) / (2.0 * size))
                        r += colour[0]
                        g += colour[1]
                        b += colour[2]
                    a += 255
            total = samples * samples
            if a == 0:
                row.extend((0, 0, 0, 0))
            else:
                # Un-premultiply: colour samples were skipped outside the tile.
                covered = a / 255
                row.extend((
                    min(255, round(r / covered)),
                    min(255, round(g / covered)),
                    min(255, round(b / covered)),
                    round(a / total),
                ))
        rows.append(bytes(row))
    return rows


def png_bytes(size, rows):
    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    width = height = size
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(images):
    """images: list of (size, png_bytes). ICO with PNG payloads (Vista+)."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    payload = b""
    for size, data in images:
        dimension = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset,
        )
        payload += data
        offset += len(data)
    return header + entries + payload


def svg_document():
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="Nodes Ops">
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2574eb"/>
      <stop offset="0.5" stop-color="#1b62d1"/>
      <stop offset="1" stop-color="#169c62"/>
    </linearGradient>
  </defs>
  <rect x="14" y="14" width="228" height="228" rx="55" fill="url(#brand)"/>
  <path fill="#fff" d="M65 65h36l54 74V65h36v126h-36l-54-74v74H65z"/>
</svg>
"""


def main():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    ico_sizes = (16, 32, 48, 64, 128, 256)
    rendered = {}
    for size in ico_sizes:
        samples = 4 if size <= 64 else 3
        rendered[size] = png_bytes(size, render_rgba(size, samples=samples))

    (STATIC_DIR / "favicon.ico").write_bytes(
        ico_bytes([(size, rendered[size]) for size in ico_sizes]),
    )
    (STATIC_DIR / "apple-touch-icon.png").write_bytes(
        png_bytes(180, render_rgba(180, samples=3)),
    )
    (STATIC_DIR / "icon-192.png").write_bytes(png_bytes(192, render_rgba(192, samples=3)))
    (STATIC_DIR / "favicon.svg").write_text(svg_document(), encoding="utf-8")

    for name in ("favicon.ico", "apple-touch-icon.png", "icon-192.png", "favicon.svg"):
        path = STATIC_DIR / name
        print(f"{path.relative_to(BASE_DIR)}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
