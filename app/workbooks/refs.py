"""A1 cell references and rectangles (specification 2.1.0 section 6).

Canonical references are uppercase A1 with no `$` and no sheet prefix; ranges always
include both endpoints (`A1:D9`). Rows and columns are one-based.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

MAX_ROW = 1_048_576
MAX_COL = 16_384
_REF = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})", re.ASCII)


class CellReferenceError(ValueError):
    """A cell or range reference is malformed or out of Excel's grid."""


def column_number(letters: str) -> int:
    value = 0
    for character in letters.upper():
        value = value * 26 + (ord(character) - 64)
    return value


def column_letters(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def parse_cell(text: str) -> tuple[int, int]:
    """(row, col) for an A1 reference, allowing `$` markers."""
    match = _REF.fullmatch(text.strip())
    if match is None:
        raise CellReferenceError(text)
    col = column_number(match.group(1))
    row = int(match.group(2))
    if not (1 <= row <= MAX_ROW and 1 <= col <= MAX_COL):
        raise CellReferenceError(text)
    return row, col


def cell_ref(row: int, col: int) -> str:
    return f"{column_letters(col)}{row}"


@dataclass(frozen=True, order=True)
class Rect:
    top: int
    left: int
    bottom: int
    right: int

    @property
    def rows(self) -> int:
        return self.bottom - self.top + 1

    @property
    def cols(self) -> int:
        return self.right - self.left + 1

    @property
    def area(self) -> int:
        return self.rows * self.cols

    def contains(self, row: int, col: int) -> bool:
        return self.top <= row <= self.bottom and self.left <= col <= self.right

    def intersects(self, other: "Rect") -> bool:
        return not (
            other.left > self.right
            or other.right < self.left
            or other.top > self.bottom
            or other.bottom < self.top
        )

    def canonical(self) -> str:
        return f"{cell_ref(self.top, self.left)}:{cell_ref(self.bottom, self.right)}"


def parse_range(text: str) -> Rect:
    """A rectangle from `A1` or `A1:D9` (endpoints in either order)."""
    first, separator, second = text.strip().partition(":")
    top, left = parse_cell(first)
    bottom, right = parse_cell(second) if separator else (top, left)
    return Rect(min(top, bottom), min(left, right), max(top, bottom), max(left, right))


def parse_sqref(text: str) -> list[Rect]:
    """Space-separated rectangles of a sqref, in stored order."""
    parts = text.split()
    if not parts:
        raise CellReferenceError(text)
    return [parse_range(part) for part in parts]


class RectIndex:
    """Point-in-any-rectangle lookups without scanning every rectangle per cell."""

    def __init__(self, rects: Iterable[Rect], budget: int = 1_000_000) -> None:
        self.rects = list(rects)
        self.by_row: dict[int, list[Rect]] = defaultdict(list)
        self.tall: list[Rect] = []
        for rect in self.rects:
            if rect.rows <= budget:
                budget -= rect.rows
                for row in range(rect.top, rect.bottom + 1):
                    self.by_row[row].append(rect)
            else:
                self.tall.append(rect)

    def contains(self, row: int, col: int) -> bool:
        return any(rect.left <= col <= rect.right for rect in self.by_row.get(row, ())) or any(
            rect.contains(row, col) for rect in self.tall
        )
