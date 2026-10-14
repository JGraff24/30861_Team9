"""Test-only fake target system following the Team 9 contract, with fault switches.

It lives under tests/ so it can never ship as a system implementation or a fake service.
"""

import hashlib
import io
import itertools
import json
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from tester.suite.identity import finding_id

BASE_URL = "http://127.0.0.1:8000"
TOKENS = {
    "userA": "fake-user-a",
    "userB": "fake-user-b",
    "reviewer": "fake-rev",
    "test-admin": "fake-admin",
}
EXPECTATIONS = Path(__file__).resolve().parents[1] / "fixtures" / "expectations.json"


def access_map() -> dict[str, dict[str, str]]:
    return {BASE_URL: dict(TOKENS)}


def address_resolver(address: str = "127.0.0.1") -> Callable[..., list[Any]]:
    def resolve(host: str, port: object, **kwargs: object) -> list[Any]:
        return [(2, 1, 6, "", (address, 0))]

    return resolve


@dataclass
class Audit:
    audit_id: str
    workbook_id: str
    version_id: str
    owner: str
    sha256: str
    file_name: str
    findings: list[dict[str, str]]
    polls_left: int


@dataclass
class FakeSystem:
    upload_limit: int = 25_000_000
    worksheet_limit: int = 50
    cell_limit: int = 250_000
    code_coverage: dict[str, Any] = field(
        default_factory=lambda: {
            "covered": 40,
            "total": 50,
            "basis": "Fake audit/validation lines.",
        }
    )
    processing_polls: int = 1
    # Faults: path -> HTTP status to return, path -> raise a transport error, custom handlers.
    status_overrides: dict[str, int] = field(default_factory=dict)
    drop_paths: set[str] = field(default_factory=set)
    setup_response: Callable[[dict[str, Any]], httpx.Response] | None = None
    mutate_findings: Callable[[list[dict[str, str]]], list[dict[str, str]]] | None = None
    gate: threading.Event | None = None
    sessions: set[str] = field(default_factory=set)
    audits: dict[str, Audit] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        raw = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
        self.expected = {fixture["sha256"]: fixture for fixture in raw["fixtures"].values()}
        self.subjects = {token: name for name, token in TOKENS.items()}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def project_error(status: int, code: str) -> httpx.Response:
        body = {
            "code": code,
            "message": code.replace("_", " ").lower(),
            "requestId": "req-1",
            "details": {},
        }
        return httpx.Response(status, json=body)

    @staticmethod
    def test_error(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"message": message, "source": "system"})

    def subject(self, request: httpx.Request) -> str | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return self.subjects.get(token) if scheme == "Bearer" else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.drop_paths:
            raise httpx.ConnectError("dropped by fake", request=request)
        if path in self.status_overrides:
            return self.test_error(self.status_overrides[path], "injected fault")
        if path == "/api/v1/health" and self.gate is not None:
            self.gate.wait(timeout=10)
        if path.startswith("/test/"):
            return self.instructor(request, path)
        session = request.headers.get("x-test-session")
        if session is not None and session not in self.sessions:
            return self.project_error(400, "UNKNOWN_TEST_SESSION")
        if path == "/api/v1/health":
            return httpx.Response(
                200, json={"status": "AVAILABLE", "apiVersion": "1.0", "rulesetVersion": "1.0.0"}
            )
        subject = self.subject(request)
        if subject is None:
            return self.project_error(401, "UNAUTHENTICATED")
        if path == "/api/v1/workbooks" and request.method == "POST":
            return self.upload(request, subject)
        return self.read(path, subject)

    def instructor(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.subject(request) != "test-admin":
            return self.test_error(401, "test-admin credential required")
        if path == "/test/setup":
            body = json.loads(request.content)
            if self.setup_response is not None:
                return self.setup_response(body)
            if body["session_id"] in self.sessions:
                return self.test_error(409, "session exists")
            self.sessions.add(body["session_id"])
            return httpx.Response(201, json={"session_id": body["session_id"]})
        session_id = parse_qs(urlsplit(str(request.url)).query).get("session_id", [""])[0]
        if session_id not in self.sessions:
            return self.test_error(404, "unknown session")
        coverage = self.code_coverage
        return httpx.Response(200, json={"session_id": session_id, "coverage": {"code": coverage}})

    def upload(self, request: httpx.Request, subject: str) -> httpx.Response:
        content_type = request.headers["content-type"].encode()
        message = BytesParser(policy=policy.HTTP).parsebytes(
            b"Content-Type: " + content_type + b"\r\n\r\n" + request.content
        )
        files = {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }
        part = files.get("file")
        if part is None:
            return self.project_error(400, "MISSING_FILE")
        name = part.get_filename() or ""
        data = part.get_payload(decode=True)
        if name.endswith(".xlsm"):
            return self.project_error(422, "MACRO_ENABLED_WORKBOOK_UNSUPPORTED")
        if not name.endswith(".xlsx"):
            return self.project_error(415, "UNSUPPORTED_FILE_TYPE")
        if len(data) > self.upload_limit:
            return self.project_error(413, "FILE_TOO_LARGE")
        try:
            package = zipfile.ZipFile(io.BytesIO(data))
            parts = {name: package.read(name) for name in package.namelist()}
        except zipfile.BadZipFile:
            return self.project_error(422, "INVALID_WORKBOOK")
        workbook = parts.get("xl/workbook.xml", b"")
        sheets = [p for name, p in parts.items() if name.startswith("xl/worksheets/sheet")]
        if b"workbookProtection" in workbook or any(b"sheetProtection" in s for s in sheets):
            return self.project_error(422, "PROTECTED_WORKBOOK")
        if workbook.count(b"<sheet ") > self.worksheet_limit:
            return self.project_error(422, "TOO_MANY_WORKSHEETS")
        if sum(s.count(b"<c r=") for s in sheets) > self.cell_limit:
            return self.project_error(422, "TOO_MANY_CELLS")
        sha = hashlib.sha256(data).hexdigest()
        fixture = self.expected.get(sha, {"findings": []})
        findings = [
            {
                "findingId": finding_id(sha, "1.0.0", f["ruleId"], f["worksheet"], f["target"]),
                **f,
                "message": "Seeded issue.",
                "recommendation": "Fix it.",
            }
            for f in fixture["findings"]
        ]
        findings.sort(key=lambda f: (f["ruleId"], f["worksheet"], f["target"]))
        if self.mutate_findings is not None:
            findings = self.mutate_findings(findings)
        number = next(self._ids)
        audit = Audit(
            f"audit-{number}",
            f"workbook-{number}",
            f"version-{number}",
            subject,
            sha,
            name,
            findings,
            self.processing_polls,
        )
        self.audits[audit.audit_id] = audit
        return httpx.Response(
            202,
            json={
                "workbookId": audit.workbook_id,
                "versionId": audit.version_id,
                "auditId": audit.audit_id,
                "status": "PROCESSING",
            },
        )

    def read(self, path: str, subject: str) -> httpx.Response:
        parts = path.strip("/").split("/")
        # api v1 workbooks {w} versions {v} [audits {a}]
        if len(parts) not in (6, 8) or parts[2] != "workbooks" or parts[4] != "versions":
            return self.project_error(404, "NOT_FOUND")
        audit = next(
            (
                a
                for a in self.audits.values()
                if (a.workbook_id, a.version_id) == (parts[3], parts[5])
            ),
            None,
        )
        if audit is None or (len(parts) == 8 and parts[7] != audit.audit_id):
            return self.project_error(404, "NOT_FOUND")
        if audit.owner != subject:
            return self.project_error(403, "FORBIDDEN")
        status = "REVIEW_REQUIRED" if audit.findings else "AUDIT_COMPLETE"
        if len(parts) == 6:
            return httpx.Response(
                200,
                json={
                    "workbookId": audit.workbook_id,
                    "versionId": audit.version_id,
                    "fileName": audit.file_name,
                    "sha256": audit.sha256,
                    "createdAt": "2026-10-14T16:00:00Z",
                    "status": "PROCESSING" if audit.polls_left > 0 else status,
                    "latestAuditId": audit.audit_id,
                    "sourceVersionId": None,
                },
            )
        if audit.polls_left > 0:
            audit.polls_left -= 1
            return httpx.Response(
                200,
                json={
                    "auditId": audit.audit_id,
                    "workbookId": audit.workbook_id,
                    "versionId": audit.version_id,
                    "rulesetVersion": "1.0.0",
                    "status": "PROCESSING",
                    "startedAt": "2026-10-14T16:00:01Z",
                    "completedAt": None,
                    "findings": [],
                    "limitations": [],
                    "error": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "auditId": audit.audit_id,
                "workbookId": audit.workbook_id,
                "versionId": audit.version_id,
                "rulesetVersion": "1.0.0",
                "status": status,
                "startedAt": "2026-10-14T16:00:01Z",
                "completedAt": "2026-10-14T16:00:03Z",
                "findings": audit.findings,
                "limitations": [],
                "error": None,
            },
        )
