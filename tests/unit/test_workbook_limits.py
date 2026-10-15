"""Dylan: every validation limit at below / equal / above with genuinely valid content.

Rejections assert details["limit"], so a different defense (XML/DTD, another limit)
can never silently become the limit under test.
"""

import io
import struct
import zipfile

import pytest
from ooxml_builder import Sheet, Workbook, cell_rows, simple_workbook, zip_parts

from app.audit.engine import OoxmlWorkbookEngine
from app.contracts.errors import WorkbookValidationError
from app.contracts.models import ProcessingLimits

ENGINE = OoxmlWorkbookEngine()
DEFAULTS = ProcessingLimits()


def padded_to(total_bytes: int) -> bytes:
    """A valid stored workbook of exactly total_bytes, padded with an innocuous .bin part."""
    wb = simple_workbook("Data")
    wb.compression = zipfile.ZIP_STORED

    def build(filler: int) -> bytes:
        wb.extra_parts = {"customXml/filler.bin": b"\x00" * filler}
        return wb.build()

    base = len(build(0))
    data = build(total_bytes - base)
    assert len(data) == total_bytes
    return data


def limit_of(data: bytes, limits: ProcessingLimits) -> tuple[int, str, object]:
    with pytest.raises(WorkbookValidationError) as caught:
        ENGINE.validate(data, "book.xlsx", limits)
    return caught.value.status_code, caught.value.code, caught.value.details.get("limit")


# -- file bytes (real 25,000,000 default) ----------------------------------------------


@pytest.mark.parametrize("delta", [-1, 0])
def test_file_size_at_or_below_default_limit_is_accepted(delta: int) -> None:
    data = padded_to(DEFAULTS.max_file_bytes + delta)
    assert (
        ENGINE.validate(data, "book.xlsx", DEFAULTS).size_bytes == DEFAULTS.max_file_bytes + delta
    )


def test_file_size_above_default_limit_is_413() -> None:
    data = padded_to(DEFAULTS.max_file_bytes + 1)
    assert limit_of(data, DEFAULTS) == (413, "FILE_TOO_LARGE", "max_file_bytes")


# -- worksheets (real default of 50) ---------------------------------------------------


@pytest.mark.parametrize("count", [49, 50])
def test_worksheets_at_or_below_default_limit_are_accepted(count: int) -> None:
    data = simple_workbook(*(f"Tab {i}" for i in range(count))).build()
    assert ENGINE.validate(data, "book.xlsx", DEFAULTS).worksheet_count == count


def test_worksheets_above_default_limit_are_rejected() -> None:
    data = simple_workbook(*(f"Tab {i}" for i in range(51))).build()
    assert limit_of(data, DEFAULTS) == (422, "WORKBOOK_LIMIT_EXCEEDED", "max_worksheets")


def test_hidden_sheets_count_toward_worksheet_limit() -> None:
    wb = simple_workbook("A", "B", "C")
    wb.sheets[2].state = "veryHidden"
    limits = ProcessingLimits(max_worksheets=2)
    assert limit_of(wb.build(), limits)[2] == "max_worksheets"


# -- nonempty cells (real default of 250,000) ------------------------------------------


def cells_workbook(*per_sheet: int, padding: int = 0) -> bytes:
    """Sheets with exact nonempty counts plus `padding` formatting-only cells (uncounted)."""
    sheets = []
    for index, count in enumerate(per_sheet, start=1):
        filler = '<row r="99999">' + '<c s="1"/>' * padding + "</row>" if padding else ""
        part = f"xl/worksheets/sheet{index}.xml"
        sheets.append(Sheet(f"Part {index}", part, str(index), cells=cell_rows(count) + filler))
    return Workbook(sheets).build()


@pytest.mark.parametrize("count", [249_999, 250_000])
def test_cells_at_or_below_default_limit_are_accepted(count: int) -> None:
    data = cells_workbook(count - 100_000, 100_000)
    assert ENGINE.validate(data, "book.xlsx", DEFAULTS).nonempty_cell_count == count


def test_cells_above_default_limit_across_sheets_are_rejected() -> None:
    data = cells_workbook(150_000, 100_001)
    assert limit_of(data, DEFAULTS) == (422, "WORKBOOK_LIMIT_EXCEEDED", "max_nonempty_cells")


def test_formatting_only_cells_do_not_reach_the_cell_limit() -> None:
    data = cells_workbook(10, padding=5_000)
    limits = ProcessingLimits(max_nonempty_cells=10)
    assert ENGINE.validate(data, "book.xlsx", limits).nonempty_cell_count == 10
    assert limit_of(data, ProcessingLimits(max_nonempty_cells=9))[2] == "max_nonempty_cells"


# -- ZIP entries -----------------------------------------------------------------------


def entries_workbook(total_entries: int) -> bytes:
    wb = simple_workbook("Data")
    base = len(wb.parts())
    wb.extra_parts = {f"customXml/f{i}.bin": b"x" for i in range(total_entries - base)}
    data = wb.build()
    assert len(zipfile.ZipFile(io.BytesIO(data)).infolist()) == total_entries
    return data


@pytest.mark.parametrize("delta,accepted", [(-1, True), (0, True), (1, False)])
def test_zip_entry_limit_boundaries(delta: int, accepted: bool) -> None:
    limits = ProcessingLimits(max_zip_entries=40)
    data = entries_workbook(40 + delta)
    if accepted:
        ENGINE.validate(data, "book.xlsx", limits)
    else:
        assert limit_of(data, limits) == (422, "WORKBOOK_LIMIT_EXCEEDED", "max_zip_entries")


def test_zip_entry_limit_at_real_default() -> None:
    assert ENGINE.validate(entries_workbook(10_000), "book.xlsx", DEFAULTS).worksheet_count == 1
    assert limit_of(entries_workbook(10_001), DEFAULTS)[2] == "max_zip_entries"


# -- expanded bytes and XML part bytes -------------------------------------------------


def expanded_total(data: bytes) -> int:
    return sum(i.file_size for i in zipfile.ZipFile(io.BytesIO(data)).infolist())


def expanded_workbook(total: int) -> bytes:
    wb = simple_workbook("Data")

    def build(filler: int) -> bytes:
        wb.extra_parts = {"customXml/filler.bin": b"\x00" * filler}
        return wb.build()

    data = build(total - expanded_total(build(0)))
    assert expanded_total(data) == total
    return data


@pytest.mark.parametrize("delta,accepted", [(-1, True), (0, True), (1, False)])
def test_expanded_byte_limit_boundaries(delta: int, accepted: bool) -> None:
    limits = ProcessingLimits(max_expanded_bytes=200_000)
    data = expanded_workbook(200_000 + delta)
    if accepted:
        ENGINE.validate(data, "book.xlsx", limits)
    else:
        assert limit_of(data, limits) == (422, "WORKBOOK_LIMIT_EXCEEDED", "max_expanded_bytes")


def test_highly_compressed_zip_bomb_hits_expanded_limit_not_file_size() -> None:
    data = expanded_workbook(DEFAULTS.max_expanded_bytes + 1)
    assert len(data) < 1_000_000  # small on the wire
    assert limit_of(data, DEFAULTS)[2] == "max_expanded_bytes"


def xml_part_workbook(part_size: int) -> bytes:
    """A worksheet whose XML part is exactly part_size bytes (padded with an XML comment)."""
    wb = simple_workbook("Data")
    parts = wb.parts()
    sheet = parts["xl/worksheets/sheet1.xml"]
    comment_overhead = len(b"<!---->")
    pad = part_size - len(sheet) - comment_overhead
    parts["xl/worksheets/sheet1.xml"] = sheet + b"<!--" + b"p" * pad + b"-->"
    assert len(parts["xl/worksheets/sheet1.xml"]) == part_size
    return zip_parts(parts)


@pytest.mark.parametrize("delta,accepted", [(-1, True), (0, True), (1, False)])
def test_xml_part_limit_boundaries(delta: int, accepted: bool) -> None:
    limits = ProcessingLimits(max_xml_part_bytes=100_000)
    data = xml_part_workbook(100_000 + delta)
    if accepted:
        ENGINE.validate(data, "book.xlsx", limits)
    else:
        assert limit_of(data, limits) == (422, "WORKBOOK_LIMIT_EXCEEDED", "max_xml_part_bytes")


def test_actual_xml_bytes_are_measured_when_declared_size_is_understated() -> None:
    """Patch the declared uncompressed sizes down; actual reads must still be bounded."""
    limits = ProcessingLimits(max_xml_part_bytes=100_000)
    data = bytearray(xml_part_workbook(150_000))
    name = b"xl/worksheets/sheet1.xml"
    for signature, size_offset, name_len_offset, name_offset in (
        (b"PK\x03\x04", 22, 26, 30),
        (b"PK\x01\x02", 24, 28, 46),
    ):
        pos = 0
        while (pos := data.find(signature, pos)) != -1:
            (length,) = struct.unpack_from("<H", data, pos + name_len_offset)
            if bytes(data[pos + name_offset : pos + name_offset + length]) == name:
                struct.pack_into("<I", data, pos + size_offset, 1_000)
            pos += 4
    with pytest.raises(WorkbookValidationError) as caught:
        ENGINE.validate(bytes(data), "book.xlsx", limits)
    # zipfile stops at the declared size, so the stream ends early: either way it is rejected.
    assert caught.value.code in {"INVALID_WORKBOOK", "WORKBOOK_LIMIT_EXCEEDED"}
