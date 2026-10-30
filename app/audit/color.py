"""W3C relative luminance and contrast ratio screening (specification section 7.7)."""

from fractions import Fraction

from app.workbooks.styles import RGB

NORMAL_MINIMUM = 4.5
LARGE_MINIMUM = 3.0


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: RGB) -> float:
    red, green, blue = (_linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: RGB, second: RGB) -> float:
    a, b = relative_luminance(first), relative_luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def is_large_text(size_points: Fraction, bold: bool) -> bool:
    return size_points >= 18 or (bold and size_points >= 14)


def passes(ratio: float, large: bool) -> bool:
    """Unrounded comparison; equality passes."""
    return ratio >= (LARGE_MINIMUM if large else NORMAL_MINIMUM)


def is_nonneutral(rgb: RGB) -> bool:
    return not rgb[0] == rgb[1] == rgb[2]
