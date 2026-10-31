"""Streaming, bounded multipart intake with one file and no temporary uploads."""

from fastapi import Request
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from app.contracts.errors import ProjectException
from app.storage.blobs import MAX_SOURCE_BYTES

MAX_MULTIPART_BYTES = MAX_SOURCE_BYTES + 65_536


async def read_upload(request: Request) -> tuple[bytes, str]:
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary", b"")
    if content_type != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise ProjectException(400, "INVALID_REQUEST", "A multipart file field is required.")
    data = bytearray()
    header_name = bytearray()
    header_value = bytearray()
    headers: dict[bytes, bytes] = {}
    filename: str | None = None
    parts = 0
    ended = False

    def malformed() -> ProjectException:
        return ProjectException(400, "INVALID_REQUEST", "Exactly one file field is required.")

    def part_begin() -> None:
        nonlocal parts
        parts += 1
        if parts > 1:
            raise malformed()

    def header_field(chunk: bytes, start: int, end: int) -> None:
        header_name.extend(chunk[start:end])

    def header_data(chunk: bytes, start: int, end: int) -> None:
        header_value.extend(chunk[start:end])

    def header_end() -> None:
        key = bytes(header_name).lower()
        if key in headers:
            raise malformed()
        headers[key] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def headers_finished() -> None:
        nonlocal filename
        disposition, attributes = parse_options_header(headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or attributes.get(b"name") != b"file":
            raise malformed()
        raw = attributes.get(b"filename")
        if not raw:
            raise malformed()
        try:
            filename = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise malformed() from exc

    def part_data(chunk: bytes, start: int, end: int) -> None:
        if len(data) + end - start > MAX_SOURCE_BYTES:
            raise ProjectException(413, "FILE_TOO_LARGE", "Upload exceeds 25,000,000 bytes.")
        data.extend(chunk[start:end])

    def finish() -> None:
        nonlocal ended
        ended = True

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": part_begin,
            "on_header_field": header_field,
            "on_header_value": header_data,
            "on_header_end": header_end,
            "on_headers_finished": headers_finished,
            "on_part_data": part_data,
            "on_end": finish,
        },
        max_header_count=8,
        max_header_size=8192,
    )
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            # Let the file callback distinguish an oversized file from excessive
            # multipart overhead, even when ASGI supplies a large single chunk.
            parser.write(chunk)
            if total > MAX_MULTIPART_BYTES:
                raise ProjectException(
                    400, "INVALID_REQUEST", "Multipart structure exceeds its limit."
                )
        parser.finalize()
    except ProjectException:
        raise
    except Exception as exc:
        raise malformed() from exc
    if not ended or filename is None or parts != 1:
        raise malformed()
    return bytes(data), filename
