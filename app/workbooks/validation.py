"""Safe intake. Check order: extension, byte size, package, structure, sheets, other XML."""

import hashlib

from app.contracts.models import ProcessingLimits, ValidatedWorkbook
from app.workbooks import errors
from app.workbooks.package import OoxmlPackage
from app.workbooks.structure import (
    WorkbookStructure,
    check_remaining_xml,
    read_workbook,
    scan_sheets,
)


def file_extension(filename: str) -> str:
    basename = filename.replace("\\", "/").rpartition("/")[2]
    return basename.rpartition(".")[2].casefold() if "." in basename else ""


def check_extension(filename: str) -> None:
    extension = file_extension(filename)
    if extension == "xlsm":
        errors.macro("xlsm_extension")
    if extension != "xlsx":
        errors.reject(
            415,
            errors.UNSUPPORTED_FILE_TYPE,
            "Only .xlsx workbooks are supported.",
            {"extension": extension[:16]},
        )


def inspect_package(
    data: bytes, limits: ProcessingLimits, *, enforce_content_limits: bool
) -> tuple[OoxmlPackage, WorkbookStructure]:
    """Open and scan the package. Raises WorkbookValidationError on any rejection."""
    if enforce_content_limits and len(data) > limits.max_file_bytes:
        errors.limit_exceeded("max_file_bytes", limits.max_file_bytes)
    if not data:
        errors.invalid("empty_file")
    package = OoxmlPackage(data, limits)
    structure = read_workbook(package, limits.max_worksheets if enforce_content_limits else None)
    scan_sheets(package, structure, limits.max_nonempty_cells if enforce_content_limits else None)
    check_remaining_xml(package)
    return package, structure


def validate_workbook(data: bytes, filename: str, limits: ProcessingLimits) -> ValidatedWorkbook:
    check_extension(filename)
    _, structure = inspect_package(data, limits, enforce_content_limits=True)
    return ValidatedWorkbook(
        source_sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        worksheet_count=structure.worksheet_count,
        nonempty_cell_count=structure.nonempty_cells,
        worksheet_parts=structure.worksheet_parts,
    )
