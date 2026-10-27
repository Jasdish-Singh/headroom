"""Pixel art for Roomy. Each frame is 112 by 88. Magenta is the transparent key."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

FRAME_W = 112
FRAME_H = 88
COLUMNS = 4
ROWS = 11
MAGENTA = (255, 0, 255)

ROW_NAMES = (
    "power",
    "happy",
    "walk",
    "tired",
    "stuffed",
    "sleep",
    "scan",
    "flicker",
    "dusty",
    "slimy",
    "sparkle",
)

# 3x5 glyphs, row-major. Uppercase plus the punctuation the screen needs.
_FONT = {
    "A": "010101111101101",
    "C": "011100100100011",
    "D": "110101101101110",
    "E": "111100110100111",
    "F": "111100110100100",
    "G": "011100101101011",
    "I": "111010010010111",
    "K": "101110100110101",
    "L": "100100100100111",
    "M": "101111101101101",
    "N": "110101101101101",
    "O": "111101101101111",
    "R": "110101110101101",
    "S": "111100111001111",
    "U": "101101101101111",
    "Y": "101101010010010",
    "Z": "111001010100111",
    "0": "111101101101111",
    "9": "111101111001001",
    "%": "101001010100101",
    "!": "010010010000010",
    ".": "000000000000010",
    ">": "100010001010100",
    " ": "000000000000000",
}


def palette(name: str) -> dict[str, tuple[int, int, int]]:
    """Beige Roomy, midnight, or the tower skin. Screen colors stay readable."""
    if name == "midnight":
        bezel, shade, edge = (61, 70, 88), (42, 48, 64), (24, 28, 38)
        stand, cord = (46, 53, 68), (22, 26, 34)
    elif name in {"roomy", "tower"}:
        bezel, shade, edge = (234, 220, 200), (196, 176, 148), (122, 104, 84)
        stand, cord = (214, 196, 170), (74, 68, 62)
    else:
        raise ValueError(f"unknown built-in palette {name}")
    return {
        "bezel": bezel,
        "shade": shade,
        "edge": edge,
        "stand": stand,
        "cord": cord,
        "screen_off": (16, 18, 24),
        "screen_happy": (18, 46, 40),
        "screen_tired": (92, 24, 34),
        "screen_stuffed": (48, 40, 28),
        "screen_sleep": (14, 18, 26),
        "screen_scan": (16, 28, 48),
        "flash": (214, 255, 226),
        "line": (170, 255, 196),
        "text": (125, 255, 179),
        "warn": (255, 107, 107),
        "amber": (255, 196, 84),
        "dim": (138, 160, 184),
        "eye": (244, 255, 240),
        "pupil": (24, 28, 32),
        "sweat": (126, 200, 227),
        "led_off": (90, 78, 70),
        "led_on": (98, 214, 122),
        "led_hot": (214, 72, 64),
        "knob": (92, 84, 76),
        "knob_hi": (210, 206, 196),
        "folder": (232, 184, 72),
        "folder_dark": (176, 124, 40),
        "phosphor": (10, 42, 22),
        "grid": (36, 128, 68),
        "sweep": (186, 255, 148),
        "blip": (232, 255, 196),
        "dust": (176, 164, 148),
        "dust_dark": (112, 102, 90),
        "slime": (88, 196, 62),
        "slime_hi": (186, 236, 120),
        "spark": (255, 236, 150),
        "spark_hot": (255, 255, 255),
        "bay": (86, 78, 68),
        "slot": (28, 26, 24),
        "scanline": (0, 0, 0),
    }


class Pix:
    def __init__(self, width: int = FRAME_W, height: int = FRAME_H) -> None:
        self.w = width
        self.h = height
        self.px = [MAGENTA] * (width * height)

    def set(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h and color != MAGENTA:
            self.px[y * self.w + x] = color

    def rect(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.set(xx, yy, color)

    def rgb(self) -> bytes:
        return b"".join(bytes(pixel) for pixel in self.px)


def _round_rect(pix: Pix, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
    pix.rect(x + 2, y, w - 4, h, color)
    pix.rect(x, y + 2, w, h - 4, color)
    pix.rect(x + 1, y + 1, w - 2, 1, color)
    pix.rect(x + 1, y + h - 2, w - 2, 1, color)


def _text(pix: Pix, x: int, y: int, text: str, color: tuple[int, int, int]) -> None:
    cursor = x
    for char in text:
        glyph = _FONT.get(char, _FONT.get(char.upper(), _FONT[" "]))
        for index, bit in enumerate(glyph):
            if bit == "1":
                pix.set(cursor + index % 3, y + index // 3, color)
        cursor += 4


def _eyes(pix: Pix, x: int, y: int, colors: dict[str, tuple[int, int, int]], *, tired: bool) -> None:
    for ox in (0, 10):
        pix.rect(x + ox, y, 5, 4, colors["eye"])
        if tired:
            pix.rect(x + ox, y, 5, 1, colors["pupil"])
            pix.rect(x + ox + 1, y + 2, 2, 2, colors["pupil"])
        else:
            pix.rect(x + ox + 2, y + 1, 2, 2, colors["pupil"])


def _sweat(pix: Pix, x: int, y: int, color: tuple[int, int, int]) -> None:
    pix.set(x + 1, y, color)
    pix.rect(x, y + 1, 3, 2, color)
    pix.set(x + 1, y + 3, color)


def _folder(pix: Pix, x: int, y: int, frame: int, colors: dict[str, tuple[int, int, int]]) -> None:
    grow = frame
    width = 12 + grow * 2
    height = 8 + grow
    pix.rect(x, y, 6, 3, colors["folder_dark"])
    pix.rect(x, y + 2, width, height, colors["folder"])
    pix.rect(x, y + 2, width, 1, colors["folder_dark"])
    if frame >= 2:
        for dx, dy in ((-3, 0), (width + 1, -1), (width + 2, 4), (-2, height)):
            pix.set(x + dx, y + dy, colors["folder"])
            pix.set(x + dx + 1, y + dy + 1, colors["folder_dark"])


def _radar(
    pix: Pix,
    sx: int,
    sy: int,
    sw: int,
    sh: int,
    colors: dict[str, tuple[int, int, int]],
    phase: int,
) -> None:
    """Green phosphor grid, a sweep, and blips. Original, not traced."""
    pix.rect(sx, sy, sw, sh, colors["phosphor"])
    for x in range(sx + 4, sx + sw - 1, 8):
        for y in range(sy + 1, sy + sh - 1):
            pix.set(x, y, colors["grid"])
    for y in range(sy + 4, sy + sh - 1, 8):
        for x in range(sx + 1, sx + sw - 1):
            pix.set(x, y, colors["grid"])
    cx = sx + sw // 2
    cy = sy + sh // 2
    vectors = ((1, -1), (1, 1), (-1, 1), (-1, -1))
    vx, vy = vectors[phase % 4]
    for step in range(1, 16):
        pix.set(cx + vx * step, cy + (vy * step) // 2, colors["sweep"])
    blips = (
        (sx + 34, sy + 7, 0),
        (sx + 40, sy + 18, 1),
        (sx + 10, sy + 20, 2),
        (sx + 12, sy + 8, 3),
    )
    for bx, by, quad in blips:
        color = colors["blip"] if quad == phase % 4 else colors["grid"]
        pix.rect(bx, by, 2, 2, color)
        if quad == phase % 4:
            pix.set(bx - 1, by, colors["sweep"])
            pix.set(bx + 2, by + 1, colors["sweep"])


def _dust(pix: Pix, phase: int, colors: dict[str, tuple[int, int, int]]) -> None:
    specks = (
        (26, 9),
        (40, 8),
        (78, 10),
        (86, 22),
        (24, 36),
        (88, 44),
        (32, 64),
        (58, 68),
        (74, 62),
        (46, 72),
    )
    for index, (x, y) in enumerate(specks):
        shift = phase if index % 3 == 0 else 0
        pix.set(x + shift, y, colors["dust"])
        pix.set(x + 1, y + 1, colors["dust_dark"])


def _slime(pix: Pix, phase: int, colors: dict[str, tuple[int, int, int]]) -> None:
    drips = ((34, 4), (56, 2), (74, 6), (28, 18))
    for index, (x, y) in enumerate(drips):
        length = 7 + (phase + index) % 3
        pix.rect(x, y, 3, length, colors["slime"])
        pix.set(x + 1, y, colors["slime_hi"])
        pix.set(x + 1, y + length, colors["slime"])
    pix.rect(42, 66 + (phase % 2), 14, 3, colors["slime"])
    pix.set(44, 65, colors["slime_hi"])
    pix.set(52, 69, colors["slime"])


def _sparkles(pix: Pix, phase: int, colors: dict[str, tuple[int, int, int]]) -> None:
    stars = ((14, 10), (96, 14), (10, 42), (100, 50), (36, 2), (72, 78), (18, 70))
    for index, (x, y) in enumerate(stars):
        if index % 4 not in {phase, (phase + 1) % 4}:
            continue
        pix.set(x, y, colors["spark_hot"])
        pix.set(x + 1, y, colors["spark"])
        pix.set(x - 1, y, colors["spark"])
        pix.set(x, y + 1, colors["spark"])
        pix.set(x, y - 1, colors["spark"])


def _scanlines(pix: Pix, x: int, y: int, w: int, h: int, every: int) -> None:
    for row in range(y, y + h, every):
        for col in range(x, x + w):
            current = pix.px[row * pix.w + col]
            if current == MAGENTA:
                continue
            pix.px[row * pix.w + col] = tuple(max(0, channel // 2) for channel in current)  # type: ignore[misc]


def _cord(pix: Pix, x: int, y: int, phase: int, colors: dict[str, tuple[int, int, int]]) -> None:
    swing = (0, 2, -1, 3)[phase % 4]
    for step in range(16):
        cx = x + step
        cy = y + step // 3 + (swing if step > 8 else 0)
        pix.set(cx, cy, colors["cord"])
        pix.set(cx, cy + 1, colors["edge"])
    tip_x = x + 16
    tip_y = y + 5 + swing
    pix.rect(tip_x, tip_y, 5, 4, colors["edge"])
    pix.rect(tip_x + 1, tip_y + 4, 1, 2, colors["cord"])
    pix.rect(tip_x + 3, tip_y + 4, 1, 2, colors["cord"])


def _monitor(
    pix: Pix,
    dy: int,
    colors: dict[str, tuple[int, int, int]],
    *,
    screen: str,
    phase: int,
    led: str,
    dx: int = 0,
) -> tuple[int, int, int, int]:
    """Draw the CRT chassis. Returns the screen rectangle."""
    del screen
    x, y = 22 + dx, 6 + dy
    _round_rect(pix, x, y, 68, 54, colors["edge"])
    _round_rect(pix, x + 2, y + 2, 64, 50, colors["bezel"])
    pix.rect(x + 4, y + 3, 40, 2, colors["shade"])
    # Two knobs and a lamp, centered under the glass. No speaker grille.
    pix.rect(x + 22, y + 44, 5, 5, colors["knob"])
    pix.set(x + 23, y + 45, colors["knob_hi"])
    pix.rect(x + 32, y + 44, 5, 5, colors["knob"])
    pix.set(x + 33, y + 45, colors["knob_hi"])
    pix.rect(x + 8, y + 45, 3, 3, colors[led])
    # Neck and foot.
    pix.rect(x + 28, y + 52, 12, 6, colors["stand"])
    _round_rect(pix, x + 16, y + 58, 36, 8, colors["stand"])
    pix.rect(x + 18, y + 64, 32, 2, colors["edge"])
    _cord(pix, x + 62, y + 36, phase, colors)
    sx, sy, sw, sh = x + 8, y + 8, 52, 32
    pix.rect(sx - 2, sy - 2, sw + 4, sh + 4, colors["edge"])
    return sx, sy, sw, sh


def _tower(
    pix: Pix,
    dy: int,
    colors: dict[str, tuple[int, int, int]],
    *,
    phase: int,
    led: str,
    dx: int = 0,
) -> tuple[int, int, int, int]:
    """A tall desktop case with the same glass size as the CRT."""
    x, y = 22 + dx, 2 + dy
    _round_rect(pix, x, y, 68, 78, colors["edge"])
    _round_rect(pix, x + 2, y + 2, 64, 74, colors["bezel"])
    pix.rect(x + 4, y + 3, 28, 2, colors["shade"])
    sx, sy, sw, sh = x + 8, y + 8, 52, 32
    pix.rect(sx - 2, sy - 2, sw + 4, sh + 4, colors["edge"])
    pix.rect(x + 8, y + 46, 52, 7, colors["bay"])
    pix.rect(x + 12, y + 48, 28, 3, colors["slot"])
    pix.rect(x + 8, y + 56, 52, 6, colors["bay"])
    pix.rect(x + 12, y + 58, 22, 2, colors["slot"])
    pix.rect(x + 10, y + 66, 3, 3, colors[led])
    pix.rect(x + 48, y + 65, 6, 5, colors["knob"])
    pix.set(x + 50, y + 66, colors["knob_hi"])
    pix.rect(x + 6, y + 76, 10, 3, colors["stand"])
    pix.rect(x + 52, y + 76, 10, 3, colors["stand"])
    _cord(pix, x + 62, y + 28, phase, colors)
    return sx, sy, sw, sh


def _paint_screen(
    pix: Pix,
    box: tuple[int, int, int, int],
    colors: dict[str, tuple[int, int, int]],
    kind: str,
    phase: int,
) -> None:
    sx, sy, sw, sh = box
    if kind == "off":
        pix.rect(sx, sy, sw, sh, colors["screen_off"])
        return
    if kind == "line":
        pix.rect(sx, sy, sw, sh, colors["screen_off"])
        pix.rect(sx + 4, sy + sh // 2, sw - 8, 1, colors["line"])
        return
    if kind == "flash":
        pix.rect(sx, sy, sw, sh, colors["flash"])
        return
    if kind == "flicker":
        pix.rect(sx, sy, sw, sh, colors["flash"] if phase % 2 == 0 else colors["screen_off"])
        return
    if kind == "happy":
        pix.rect(sx, sy, sw, sh, colors["screen_happy"])
        _scanlines(pix, sx, sy, sw, sh, 4)
        _eyes(pix, sx + 14, sy + 6, colors, tired=False)
        label = "READY.>" if phase % 2 == 0 else "READY."
        _text(pix, sx + 8, sy + 22, label, colors["text"])
        return
    if kind == "tired":
        pix.rect(sx, sy, sw, sh, colors["screen_tired"])
        _eyes(pix, sx + 14, sy + 6, colors, tired=True)
        _text(pix, sx + 6, sy + 22, "RAM 90%!", colors["warn"])
        _sweat(pix, sx + sw - 8, sy - 8 + phase, colors["sweat"])
        _sweat(pix, sx + sw + 2, sy - 4 + (phase % 3), colors["sweat"])
        return
    if kind == "stuffed":
        pix.rect(sx, sy, sw, sh, colors["screen_stuffed"])
        _folder(pix, sx + 8, sy + 4, phase, colors)
        _text(pix, sx + 4, sy + 22, "DISK FULL", colors["amber"])
        return
    if kind == "sleep":
        pix.rect(sx, sy, sw, sh, colors["screen_sleep"])
        _scanlines(pix, sx, sy, sw, sh, 2)
        _text(pix, sx + 16, sy + 8 - phase, "Z", colors["dim"])
        _text(pix, sx + 24, sy + 14 - (phase % 3), "Z", colors["dim"])
        _text(pix, sx + 32, sy + 18, "Z", colors["eye"])
        return
    if kind == "scan":
        _radar(pix, sx, sy, sw, sh, colors, phase)
        return
    pix.rect(sx, sy, sw, sh, colors["screen_off"])


def render_frame(skin: str, row: int, column: int) -> Pix:
    colors = palette(skin)
    pix = Pix()
    phase = column % 4
    overlay = ""
    dx = 0
    if row == 0:
        kind = ("off", "line", "flash", "happy")[phase]
        dy = 0
        led = "led_off" if phase == 0 else "led_on"
        cord_phase = 0
    elif row == 1:
        kind = "happy"
        dy = (0, -1, 0, 1)[phase]
        led = "led_on"
        cord_phase = phase
    elif row == 2:
        kind = "happy"
        dy = (0, -3, -5, -2)[phase]
        led = "led_on"
        cord_phase = (0, 2, 1, 3)[phase]
    elif row == 3:
        kind = "tired"
        dy = (0, -1, 0, 0)[phase]
        led = "led_hot"
        cord_phase = phase
    elif row == 4:
        kind = "stuffed"
        dy = (0, 1, 0, 1)[phase]
        led = "led_hot"
        cord_phase = phase
    elif row == 5:
        kind = "sleep"
        dy = 0
        led = "led_off"
        cord_phase = 0
    elif row == 6:
        kind = "scan"
        dy = 0
        led = "led_on"
        cord_phase = phase
    elif row == 7:
        kind = "flicker"
        dy = 0
        led = "led_on"
        cord_phase = 0
    elif row == 8:
        kind = "happy"
        dy = (0, 0, 1, 0)[phase]
        led = "led_on"
        cord_phase = phase
        overlay = "dusty"
    elif row == 9:
        kind = "happy"
        dy = (0, 1, 0, 1)[phase]
        led = "led_on"
        cord_phase = phase
        overlay = "slimy"
    elif row == 10:
        kind = "happy"
        dy = (0, -1, 1, 0)[phase]
        dx = (0, 2, -2, 1)[phase]
        led = "led_on"
        cord_phase = phase
        overlay = "sparkle"
    else:
        kind = "flicker"
        dy = 0
        led = "led_on"
        cord_phase = 0
    if skin == "tower":
        box = _tower(pix, dy, colors, phase=cord_phase, led=led, dx=dx)
    else:
        box = _monitor(pix, dy, colors, screen=kind, phase=cord_phase, led=led, dx=dx)
    _paint_screen(pix, box, colors, kind, phase)
    if overlay == "dusty":
        _dust(pix, phase, colors)
    elif overlay == "slimy":
        _slime(pix, phase, colors)
    elif overlay == "sparkle":
        _sparkles(pix, phase, colors)
    return pix


def render_sheet(skin: str) -> tuple[int, int, bytes]:
    width = FRAME_W * COLUMNS
    height = FRAME_H * ROWS
    sheet = Pix(width, height)
    for row in range(ROWS):
        for column in range(COLUMNS):
            frame = render_frame(skin, row, column)
            ox = column * FRAME_W
            oy = row * FRAME_H
            for y in range(FRAME_H):
                for x in range(FRAME_W):
                    sheet.px[(oy + y) * width + (ox + x)] = frame.px[y * FRAME_W + x]
    return width, height, sheet.rgb()


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    raw = b"".join(b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def read_png(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos = 8
    width = height = None
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height = struct.unpack(">II", chunk[:8])
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
    if width is None or height is None:
        raise ValueError("png missing IHDR")
    raw = zlib.decompress(idat)
    stride = width * 3
    rows = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        if raw[start] != 0:
            raise ValueError("only uncompressed png filters are read")
        rows.extend(raw[start + 1 : start + 1 + stride])
    return width, height, bytes(rows)


def write_assets(directory: Path) -> None:
    """Write the three built-in sprite sheets. Run as ``python -m headroom.pets.draw``."""
    directory.mkdir(parents=True, exist_ok=True)
    for skin in ("roomy", "midnight", "tower"):
        width, height, rgb = render_sheet(skin)
        write_png(directory / f"{skin}.png", width, height, rgb)


if __name__ == "__main__":
    write_assets(Path(__file__).resolve().parent)
