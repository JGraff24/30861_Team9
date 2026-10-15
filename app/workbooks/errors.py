"""Validation rejection codes. Details never contain workbook content or entry names."""

from typing import NoReturn

from pydantic import JsonValue

from app.contracts.errors import WorkbookValidationError

UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
MACRO_ENABLED = "MACRO_ENABLED_WORKBOOK_UNSUPPORTED"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
LIMIT_EXCEEDED = "WORKBOOK_LIMIT_EXCEEDED"
INVALID_WORKBOOK = "INVALID_WORKBOOK"
UNSAFE_PACKAGE = "UNSAFE_WORKBOOK_PACKAGE"
ENCRYPTED = "ENCRYPTED_WORKBOOK_UNSUPPORTED"
PROTECTED = "PROTECTED_WORKBOOK_UNSUPPORTED"


def reject(
    status_code: int, code: str, message: str, details: dict[str, JsonValue] | None = None
) -> NoReturn:
    raise WorkbookValidationError(status_code, code, message, details or {})


def invalid(reason: str, message: str = "The file is not a readable .xlsx workbook.") -> NoReturn:
    reject(422, INVALID_WORKBOOK, message, {"reason": reason})


def unsafe(reason: str) -> NoReturn:
    reject(422, UNSAFE_PACKAGE, "The workbook package contains unsafe content.", {"reason": reason})


def macro(reason: str) -> NoReturn:
    reject(
        422,
        MACRO_ENABLED,
        "Macro-enabled workbooks are not supported. Upload a macro-free .xlsx copy.",
        {"reason": reason},
    )


def protected(scope: str) -> NoReturn:
    reject(
        422,
        PROTECTED,
        "Password-protected or locked workbooks and worksheets are not supported. "
        "Remove protection from a copy and upload it again.",
        {"scope": scope},
    )


def limit_exceeded(limit: str, maximum: int) -> NoReturn:
    if limit == "max_file_bytes":
        reject(
            413,
            FILE_TOO_LARGE,
            f"The file exceeds the {maximum}-byte upload limit.",
            {"limit": limit, "maximum": maximum},
        )
    reject(
        422,
        LIMIT_EXCEEDED,
        "The workbook exceeds a processing limit.",
        {"limit": limit, "maximum": maximum},
    )
