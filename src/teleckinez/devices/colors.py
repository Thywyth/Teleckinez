"""Named catalogue colors <-> what firmwares speak: #RRGGBB (Tasmota) and CIE xy (Zigbee)."""

import math

COLOR_HEX: dict[str, str] = {
    "white": "FFFFFF",
    "warm": "FFD9A0",
    "red": "FF0000",
    "orange": "FF8000",
    "yellow": "FFFF00",
    "green": "00FF00",
    "cyan": "00FFFF",
    "blue": "0000FF",
    "purple": "8000FF",
    "pink": "FF00A0",
}


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")[:6]
    if len(value) != 6:
        raise ValueError(f"bad hex color {value!r}")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def nearest_color(rgb: tuple[int, int, int]) -> str:
    """Catalogue color closest to an RGB triple (by hue first, so dimmed colors still match)."""

    def hue(c: tuple[int, int, int]) -> tuple[float, float]:
        r, g, b = (x / 255 for x in c)
        mx, mn = max(r, g, b), min(r, g, b)
        sat = 0.0 if mx == 0 else (mx - mn) / mx
        if mx == mn:
            return 0.0, sat
        if mx == r:
            h = ((g - b) / (mx - mn)) % 6
        elif mx == g:
            h = (b - r) / (mx - mn) + 2
        else:
            h = (r - g) / (mx - mn) + 4
        return h * 60, sat

    h, s = hue(rgb)

    def distance(name: str) -> float:
        ch, cs = hue(hex_to_rgb(COLOR_HEX[name]))
        dh = min(abs(h - ch), 360 - abs(h - ch)) / 180  # 0..1
        return dh * min(s, cs) + abs(s - cs)  # hue only matters for saturated colors

    return min(COLOR_HEX, key=distance)


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_xy(rgb: tuple[int, int, int]) -> tuple[float, float]:
    """sRGB (D65) -> CIE 1931 xy, as Zigbee lights expect."""
    r, g, b = (_linear(c / 255) for c in rgb)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = x + y + z
    if total == 0:
        return 0.3127, 0.3290  # D65 white point
    return round(x / total, 4), round(y / total, 4)


def color_to_xy(name: str) -> dict[str, float]:
    x, y = rgb_to_xy(hex_to_rgb(COLOR_HEX[name]))
    return {"x": x, "y": y}


def xy_to_color(x: float, y: float) -> str:
    """Nearest catalogue color to a reported xy point."""
    return min(COLOR_HEX, key=lambda n: math.dist((x, y), rgb_to_xy(hex_to_rgb(COLOR_HEX[n]))))
