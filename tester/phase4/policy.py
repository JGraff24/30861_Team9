"""The tester's own reading of the Phase 4 screening policy (§6, §7), written from the text.

These helpers let the tester check frozen policy vectors and recompute oracle facts (for example
a contrast ratio or a canonical range) while reviewing fixture expectations. They are not an
audit engine and never produce a fixture's expected findings automatically: expected findings
stay hand-written from each fixture's XML (§15.2).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction

# §6: Unicode White_Space for rule trimming. U+200B is deliberately absent.
WHITE_SPACE = frozenset("\u0009\u000a\u000b\u000c\u000d \u0085                 　")
RULE_ORDER = (
    "ALT_TEXT",
    "WORKSHEET_NAME",
    "TABLE_HEADER",
    "MERGED_CELLS",
    "LINK_TEXT",
    "COLOR_CONTRAST",
)
CATEGORY = {
    "ALT_TEXT": "NON_TEXT_CONTENT",
    "WORKSHEET_NAME": "WORKSHEET_NAME",
    "TABLE_HEADER": "TABLE_STRUCTURE",
    "MERGED_CELLS": "MERGED_CELLS",
    "LINK_TEXT": "LINK_TEXT",
    "COLOR_CONTRAST": "COLOR_OR_CONTRAST",
}
AMBIGUOUS_LABELS = frozenset(
    {"click here", "here", "link", "more", "read more", "learn more", "website", "url"}
)
RAW_ADDRESS_PREFIXES = ("http://", "https://", "www.", "mailto:")
THEME_INDEX_NAMES = (
    "lt1",
    "dk1",
    "lt2",
    "dk2",
    "accent1",
    "accent2",
    "accent3",
    "accent4",
    "accent5",
    "accent6",
    "hlink",
    "folHlink",
)
_GENERIC = re.compile(r"[Ss][Hh][Ee][Ee][Tt]([0-9]+)")
_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
MAX_ROW = 1_048_576
MAX_COLUMN = 16_384


def trim(text: str) -> str:
    start, end = 0, len(text)
    while start < end and text[start] in WHITE_SPACE:
        start += 1
    while end > start and text[end - 1] in WHITE_SPACE:
        end -= 1
    return text[start:end]


def collapse(text: str) -> str:
    """Trim, then replace each internal White_Space run with one ASCII space."""
    words: list[str] = []
    current: list[str] = []
    for char in trim(text):
        if char in WHITE_SPACE:
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append("".join(current))
    return " ".join(words)


def is_blank(text: str) -> bool:
    return trim(text) == ""


# --- §7.3 worksheet names -------------------------------------------------------------------


def is_generic_sheet_name(name: str) -> bool:
    """ASCII case-insensitive `Sheet` + ASCII digits with value > 0, after White_Space trim."""
    match = _GENERIC.fullmatch(trim(name))
    return match is not None and int(match.group(1)) > 0


# --- §7.6 hyperlink labels ------------------------------------------------------------------


def normalized_label(label: str) -> str:
    return collapse(label).casefold()


def is_ambiguous_label(label: str, destination: str | None = None) -> bool:
    """Blank, finite vocabulary (one trailing . ! ? ignored), raw-address prefix or destination.

    Interpretation recorded for pod review: the "raw destination text" comparison uses the same
    trimmed, whitespace-collapsed, casefolded label against the trimmed, casefolded destination.
    """
    normalized = normalized_label(label)
    if not normalized:
        return True
    vocabulary_form = normalized[:-1] if normalized[-1] in ".!?" else normalized
    if vocabulary_form in AMBIGUOUS_LABELS:
        return True
    if normalized.startswith(RAW_ADDRESS_PREFIXES):
        return True
    return destination is not None and normalized == trim(destination).casefold()


# --- §7.7 color and contrast ----------------------------------------------------------------


def parse_rgb(value: str) -> tuple[int, int, int]:
    """6-digit RGB or 8-digit ARGB (alpha ignored for SpreadsheetML cell colors)."""
    if not re.fullmatch(r"[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8}", value):
        raise ValueError("color must be 6 or 8 hexadecimal digits")
    digits = value[-6:]
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def hex_rgb(rgb: tuple[int, int, int]) -> str:
    return "".join(f"{channel:02X}" for channel in rgb)


def _round_half_up(value: Fraction) -> int:
    return (value + Fraction(1, 2)).numerator // (value + Fraction(1, 2)).denominator


def apply_tint(rgb: tuple[int, int, int], tint: float) -> tuple[int, int, int]:
    """HSL lightness tint: L*(1+t) for t<0, L*(1-t)+t for t>0; clamp; round half up.

    The corrected pack 2.1.1 requires mathematical half-up results, including exact channel
    halves. All intermediates are rational; decimal tint text is represented exactly. This
    independent oracle neither imports production math nor uses a blanket rounding epsilon.
    """
    if not -1 <= tint <= 1:
        raise ValueError("tint must be within [-1, 1]")
    red, green, blue = (Fraction(channel, 255) for channel in rgb)
    high, low = max(red, green, blue), min(red, green, blue)
    lightness = (high + low) / 2
    delta = high - low
    saturation = delta / (1 - abs(2 * lightness - 1)) if delta else Fraction(0)
    if not delta:
        sector = Fraction(0)
    elif high == red:
        sector = ((green - blue) / delta) % 6
    elif high == green:
        sector = (blue - red) / delta + 2
    else:
        sector = (red - green) / delta + 4
    amount = Fraction(str(tint))
    lightness = lightness * (1 + amount) if amount < 0 else lightness * (1 - amount) + amount
    chroma = (1 - abs(2 * lightness - 1)) * saturation
    secondary = chroma * (1 - abs(sector % 2 - 1))
    zero = Fraction(0)
    candidates = (
        (chroma, secondary, zero),
        (secondary, chroma, zero),
        (zero, chroma, secondary),
        (zero, secondary, chroma),
        (secondary, zero, chroma),
        (chroma, zero, secondary),
    )
    offset = lightness - chroma / 2
    channels = candidates[int(sector)]
    output_red, output_green, output_blue = (
        _round_half_up(min(Fraction(1), max(zero, channel + offset)) * 255) for channel in channels
    )
    return output_red, output_green, output_blue


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def is_large_text(size_points: float, bold: bool) -> bool:
    return size_points >= 18 or (bold and size_points >= 14)


def contrast_passes(ratio: float, *, large: bool) -> bool:
    """Compare the unrounded ratio; equality passes."""
    return ratio >= (3.0 if large else 4.5)


def is_nonneutral(rgb: tuple[int, int, int]) -> bool:
    return len(set(rgb)) > 1


# --- §6 cell references ---------------------------------------------------------------------


def column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + (ord(letter) - 64)
    return number


def column_letters(number: int) -> str:
    if not 1 <= number <= MAX_COLUMN:
        raise ValueError("column out of Excel bounds")
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def parse_cell(reference: str) -> tuple[int, int]:
    """Canonical uppercase A1 (no `$`, no sheet) to (row, column)."""
    match = _CELL.fullmatch(reference)
    if match is None:
        raise ValueError(f"not a canonical cell reference: {reference!r}")
    row, column = int(match.group(2)), column_number(match.group(1))
    if row > MAX_ROW or column > MAX_COLUMN:
        raise ValueError(f"cell reference out of Excel bounds: {reference!r}")
    return row, column


def cell_reference(row: int, column: int) -> str:
    if not 1 <= row <= MAX_ROW:
        raise ValueError("row out of Excel bounds")
    return f"{column_letters(column)}{row}"


def range_reference(top: int, left: int, bottom: int, right: int) -> str:
    """Canonical rectangle `A1:D9`, always with both endpoints."""
    return f"{cell_reference(top, left)}:{cell_reference(bottom, right)}"


# --- §7.4 unstructured table candidates -----------------------------------------------------


@dataclass(frozen=True)
class Component:
    top: int
    left: int
    bottom: int
    right: int
    cells: frozenset[tuple[int, int]]

    @property
    def rows(self) -> int:
        return self.bottom - self.top + 1

    @property
    def columns(self) -> int:
        return self.right - self.left + 1

    @property
    def dense(self) -> bool:
        return len(self.cells) == self.rows * self.columns

    @property
    def candidate(self) -> bool:
        return self.rows >= 2 and self.columns >= 2

    @property
    def range(self) -> str:
        return range_reference(self.top, self.left, self.bottom, self.right)


def components(
    populated: Iterable[tuple[int, int]],
    tables: Iterable[tuple[int, int, int, int]] = (),
) -> list[Component]:
    """Four-neighbor components of populated cells outside formal table rectangles.

    Sparse sets only: the cost is proportional to populated cells, never to a used-range area.
    Diagonal contact does not connect. Output is sorted by (top, left).
    """
    covered = list(tables)
    remaining = {
        cell
        for cell in populated
        if not any(t <= cell[0] <= b and lft <= cell[1] <= r for t, lft, b, r in covered)
    }
    found: list[Component] = []
    while remaining:
        start = min(remaining)
        stack, members = [start], {start}
        remaining.discard(start)
        while stack:
            row, column = stack.pop()
            for neighbor in (
                (row - 1, column),
                (row + 1, column),
                (row, column - 1),
                (row, column + 1),
            ):
                if neighbor in remaining:
                    remaining.discard(neighbor)
                    members.add(neighbor)
                    stack.append(neighbor)
        rows = [cell[0] for cell in members]
        columns = [cell[1] for cell in members]
        found.append(
            Component(min(rows), min(columns), max(rows), max(columns), frozenset(members))
        )
    return sorted(found, key=lambda component: (component.top, component.left))


CellValue = str | int | float | bool


def candidate_classification(
    component: Component, values: Mapping[tuple[int, int], CellValue]
) -> str | None:
    """None when not a candidate; else REQUIRES_USER_INPUT only for the simple safe case.

    This covers only facts visible in plain values: density, distinct nonblank plain-string
    first-row labels of at most 255 code points and a body row. Formulas, rich text, merges,
    overlaps and attached features (§8.6) must be ruled out separately by the reviewer.
    """
    if not component.candidate:
        return None
    if not component.dense:
        return "MANUAL_REVIEW"
    labels = [
        values.get((component.top, column)) for column in range(component.left, component.right + 1)
    ]
    if not all(
        isinstance(label, str) and not is_blank(label) and len(label) <= 255 for label in labels
    ):
        return "MANUAL_REVIEW"
    keys = [trim(str(label)).casefold() for label in labels]
    if len(keys) != len(set(keys)):
        return "MANUAL_REVIEW"
    return "REQUIRES_USER_INPUT"


# --- §7 finding order -----------------------------------------------------------------------


def finding_sort_key(rule_id: str, worksheet: str, target: str) -> tuple[str, str, str]:
    """Python str comparison is Unicode code-point order, as §7 requires."""
    return rule_id, worksheet, target
