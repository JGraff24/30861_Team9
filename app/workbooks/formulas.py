"""Formula text scanning: sheet-reference tokens and literal HYPERLINK calls.

Formulas are never evaluated. The scanner only recognizes the lexical forms needed by
the LINK_TEXT screen and the worksheet-rename profile (sections 7.6 and 8.4). Anything it
cannot classify with certainty is reported as unsupported so callers can choose manual
review instead of guessing.
"""

import re
from dataclasses import dataclass

_NAME_CHAR = re.compile(r"[A-Za-z0-9_.\\\u0080-\U0010FFFF]")
_NAME_START = re.compile(r"[A-Za-z_\\\u0080-\U0010FFFF]")
_FUNCTION = re.compile(r"((?:_xlfn\.|_xlws\.)?[A-Za-z][A-Za-z0-9_.]*)\s*\(", re.ASCII)
_HYPERLINK = re.compile(r"(?:_xlfn\.)?HYPERLINK", re.IGNORECASE | re.ASCII)
REFERENCE_CONSTRUCTORS = frozenset({"INDIRECT", "EVALUATE"})


@dataclass(frozen=True)
class SheetToken:
    start: int
    end: int  # exclusive; covers the trailing "!"
    names: tuple[str, ...]  # one name, or two for a 3D range Sheet1:Sheet3!
    external: bool


@dataclass(frozen=True)
class FormulaScan:
    sheet_tokens: tuple[SheetToken, ...]
    functions: tuple[str, ...]  # upper-case names, without _xlfn./_xlws. prefixes
    unsupported: str | None  # why the formula text could not be tokenized safely


def _read_quoted(text: str, index: int, quote: str) -> int | None:
    """Index just past a closing quote (doubled quotes escape), or None if unterminated."""
    index += 1
    while index < len(text):
        if text[index] == quote:
            if index + 1 < len(text) and text[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return None


def unquote_sheet(token: str) -> str:
    return token[1:-1].replace("''", "'") if token.startswith("'") else token


def quote_sheet(name: str) -> str:
    """Rewritten references always quote the new name and double embedded apostrophes."""
    return "'" + name.replace("'", "''") + "'"


def scan_formula(text: str) -> FormulaScan:
    tokens: list[SheetToken] = []
    functions: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == '"':
            end = _read_quoted(text, index, '"')
            if end is None:
                return FormulaScan(tuple(tokens), tuple(functions), "unterminated string")
            index = end
            continue
        if character == "#":
            match = re.match(r"#[A-Za-z0-9/]+[!?]?", text[index:])
            index += len(match.group(0)) if match else 1
            continue
        if character == "[":
            depth, end = 0, index
            while end < length:
                if text[end] == "[":
                    depth += 1
                elif text[end] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            if end >= length:
                return FormulaScan(tuple(tokens), tuple(functions), "unbalanced brackets")
            preceded = index > 0 and _NAME_CHAR.match(text[index - 1]) is not None
            if not preceded and end + 1 < length and _NAME_START.match(text[end + 1]):
                # [n]Sheet!A1: an external workbook reference.
                tail = re.match(r"[^!\s(),;]*!", text[end + 1 :])
                if tail is not None:
                    finish = end + 1 + len(tail.group(0))
                    tokens.append(SheetToken(index, finish, (text[end + 1 : finish - 1],), True))
                    index = finish
                    continue
            index = end + 1
            continue
        if character == "'":
            end = _read_quoted(text, index, "'")
            if end is None:
                return FormulaScan(tuple(tokens), tuple(functions), "unterminated sheet name")
            first = unquote_sheet(text[index:end])
            names = [first]
            cursor = end
            if cursor < length and text[cursor] == ":":
                second_end = None
                if cursor + 1 < length and text[cursor + 1] == "'":
                    second_end = _read_quoted(text, cursor + 1, "'")
                    if second_end is not None:
                        names.append(unquote_sheet(text[cursor + 1 : second_end]))
                if second_end is None:
                    return FormulaScan(tuple(tokens), tuple(functions), "unsupported quoted range")
                cursor = second_end
            if cursor >= length or text[cursor] != "!":
                return FormulaScan(
                    tuple(tokens), tuple(functions), "quoted text outside a reference"
                )
            external = first.startswith("[")
            tokens.append(SheetToken(index, cursor + 1, tuple(names), external))
            index = cursor + 1
            continue
        if _NAME_START.match(character) and (index == 0 or not _NAME_CHAR.match(text[index - 1])):
            end = index
            while end < length and _NAME_CHAR.match(text[end]):
                end += 1
            word = text[index:end]
            if end < length and text[end] == "!":
                tokens.append(SheetToken(index, end + 1, (word,), False))
                index = end + 1
                continue
            if end < length and text[end] == ":":
                match = re.match(r":([A-Za-z_\\\u0080-\U0010FFFF][^!\s(),;:]*)!", text[end:])
                if match is not None:
                    finish = end + len(match.group(0))
                    tokens.append(SheetToken(index, finish, (word, match.group(1)), False))
                    index = finish
                    continue
            call = _FUNCTION.match(text, index)
            if call is not None and call.end(1) == end:
                name = call.group(1).upper()
                for prefix in ("_XLFN.", "_XLWS."):
                    name = name.removeprefix(prefix)
                functions.append(name)
            index = end
            continue
        index += 1
    return FormulaScan(tuple(tokens), tuple(functions), None)


@dataclass(frozen=True)
class RenameResult:
    text: str
    changed: bool


def rewrite_sheet_references(text: str, old: str, new: str) -> RenameResult:
    """Replace single-sheet reference prefixes naming `old` (case-insensitive like Excel)."""
    scan = scan_formula(text)
    if scan.unsupported is not None:
        raise ValueError(scan.unsupported)
    pieces: list[str] = []
    cursor = 0
    changed = False
    for token in scan.sheet_tokens:
        if token.external or len(token.names) != 1:
            if any(name.casefold() == old.casefold() for name in token.names):
                raise ValueError("unsupported reference to the renamed sheet")
            continue
        if token.names[0].casefold() != old.casefold():
            continue
        pieces.append(text[cursor : token.start])
        pieces.append(quote_sheet(new) + "!")
        cursor = token.end
        changed = True
    pieces.append(text[cursor:])
    return RenameResult("".join(pieces), changed)


@dataclass(frozen=True)
class HyperlinkCall:
    """A HYPERLINK formula. `label` is None when the label is not a screenable literal."""

    literal: bool
    label: str | None
    destination: str | None


def _strip_strings(text: str) -> str:
    return re.sub(r'"(?:[^"]|"")*"', '""', text)


def contains_hyperlink(text: str) -> bool:
    stripped = _strip_strings(text)
    for match in _HYPERLINK.finditer(stripped):
        before = stripped[match.start() - 1] if match.start() else ""
        after = stripped[match.end() :].lstrip()
        if (not before or not _NAME_CHAR.match(before)) and after.startswith("("):
            return True
    return False


_LITERAL_CALL = re.compile(
    r'\s*=?\s*(?:_xlfn\.)?HYPERLINK\s*\(\s*("(?:[^"]|"")*")\s*(?:,\s*("(?:[^"]|"")*")\s*)?\)\s*',
    re.IGNORECASE | re.ASCII,
)


def parse_hyperlink(text: str) -> HyperlinkCall | None:
    """None when the formula has no HYPERLINK call at all."""
    if not contains_hyperlink(text):
        return None
    match = _LITERAL_CALL.fullmatch(text)
    if match is None:
        return HyperlinkCall(False, None, None)
    destination = match.group(1)[1:-1].replace('""', '"')
    label_token = match.group(2)
    label = label_token[1:-1].replace('""', '"') if label_token is not None else destination
    return HyperlinkCall(True, label, destination)
