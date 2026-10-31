"""Test-only fake Phase 4 target (specification 2.1.0) for exercising the independent tester.

It lives under tests/ and must never be deployed or registered as a system. Its "audit"
returns the hand-written expectations for known fixture hashes, and its "editor" performs
string-level edits that only understand Team 9's own synthetic fixture XML. Mutation switches
let unit tests prove the tester notices wrong behavior.
"""

import copy
import hashlib
import io
import itertools
import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from xml.sax.saxutils import escape, unescape

import httpx

from tester.phase4.policy import CATEGORY, RULE_ORDER, is_ambiguous_label, is_generic_sheet_name
from tester.suite.identity import finding_id

BASE_URL = "http://127.0.0.1:8000"
TOKENS = {
    "userA": "fake-user-a",
    "userB": "fake-user-b",
    "reviewer": "fake-rev",
    "test-admin": "fake-admin",
}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXPECTATIONS = FIXTURES / "expectations-phase4.json"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
RULESET = "2.0.0"
START = datetime(2026, 10, 29, 16, 0, 0, tzinfo=UTC)
FORBIDDEN_NAME = re.compile(r"[:\\/?*\[\]\x00-\x1f]")


def access_map() -> dict[str, dict[str, str]]:
    return {BASE_URL: dict(TOKENS)}


class Reply(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response


def project_error(status: int, code: str, details: dict[str, Any] | None = None) -> Reply:
    body = {
        "code": code,
        "message": code.replace("_", " ").capitalize() + ".",
        "requestId": "req-fake",
        "details": details or {},
    }
    return Reply(httpx.Response(status, json=body, headers={"X-Request-ID": "req-fake"}))


def test_error(status: int, message: str) -> Reply:
    return Reply(httpx.Response(status, json={"message": message, "source": "system"}))


@dataclass
class Audit:
    audit_id: str
    version_id: str
    status: str
    started: str
    completed: str | None
    findings: list[dict[str, str]]
    limitations: list[str]
    error: dict[str, Any] | None
    polls_left: int
    report: bytes | None = None
    review: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Version:
    version_id: str
    workbook_id: str
    file_name: str
    data: bytes
    created: str
    source_version_id: str | None
    remediation_id: str | None = None
    expected: dict[str, Any] | None = None
    audits: list[str] = field(default_factory=list)
    stable_status: str = "PROCESSING"


@dataclass
class Workbook:
    workbook_id: str
    owner: str
    namespace: str | None
    versions: list[str] = field(default_factory=list)


@dataclass
class Remediation:
    remediation_id: str
    workbook_id: str
    source_version_id: str
    source_audit_id: str
    decisions: list[dict[str, Any]]
    polls_left: int
    status: str = "REMEDIATING"
    output_version_id: str | None = None
    output_id: str | None = None
    audit_id: str | None = None
    error: dict[str, Any] | None = None


@dataclass
class FakePhase4:
    fault_switch: bool = True
    upload_limit: int = 25_000_000
    worksheet_limit: int = 50
    cell_limit: int = 250_000
    processing_polls: int = 1
    remediation_polls: int = 1
    reviewer_grants: dict[str, set[str]] = field(default_factory=lambda: {"reviewer": {"userA"}})
    code_coverage: dict[str, Any] = field(
        default_factory=lambda: {"covered": 120, "total": 400, "basis": "Fake measured lines."}
    )
    # Mutation switches used by unit tests to prove the tester catches defects.
    mutate_output: Callable[[dict[str, bytes]], None] | None = None
    keep_applied_finding: bool = False
    accept_manual_apply: bool = False
    duplicate_setup_extra_field: bool = False
    corrupt_output: bool = False
    fail_audits_for: set[str] = field(default_factory=set)  # upload file names
    fractional_times: bool = False
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    workbooks: dict[str, Workbook] = field(default_factory=dict)
    versions: dict[str, Version] = field(default_factory=dict)
    audits: dict[str, Audit] = field(default_factory=dict)
    remediations: dict[str, Remediation] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    keys: dict[tuple[str, str | None, str, str], tuple[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    requests: list[httpx.Request] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _tick: Any = field(default_factory=lambda: itertools.count(0))

    def __post_init__(self) -> None:
        raw = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))
        self.expected = {
            fixture["sha256"]: fixture
            for fixture in raw["fixtures"].values()
            if fixture["kind"] != "pending"
        }
        self.subjects = {token: name for name, token in TOKENS.items()}

    # --- plumbing --------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def now(self) -> str:
        moment = START + timedelta(seconds=next(self._tick))
        if self.fractional_times and moment.second % 2:
            moment += timedelta(milliseconds=123)
        return moment.isoformat().replace("+00:00", "Z")

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def subject(self, request: httpx.Request) -> str | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return self.subjects.get(token) if scheme == "Bearer" else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            return self.route(request)
        except Reply as reply:
            return reply.response

    def route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/test/"):
            return self.instructor(request, path)
        if path == "/api/v1/health":
            return httpx.Response(
                200, json={"status": "AVAILABLE", "apiVersion": "1.0", "rulesetVersion": RULESET}
            )
        namespace = request.headers.get("x-test-session")
        if namespace is not None and namespace not in self.sessions:
            raise project_error(400, "INVALID_TEST_SESSION")
        subject = self.subject(request)
        if subject is None:
            raise project_error(401, "UNAUTHORIZED")
        if subject == "test-admin":
            raise project_error(403, "FORBIDDEN")
        parts = path.strip("/").split("/")
        method = request.method
        if parts[:3] != ["api", "v1", "workbooks"] and parts[:3] not in (
            ["api", "v1", "remediations"],
            ["api", "v1", "outputs"],
        ):
            raise project_error(404, "NOT_FOUND")
        if parts[2] == "remediations" and len(parts) == 4 and method == "GET":
            return self.remediation_status(parts[3], subject, namespace)
        if parts[2] == "outputs" and len(parts) == 5 and parts[4] == "workbook" and method == "GET":
            return self.output_download(parts[3], subject, namespace)
        if parts[2] != "workbooks":
            raise project_error(404, "NOT_FOUND")
        if len(parts) == 3 and method == "POST":
            return self.upload(request, subject, namespace, None)
        if len(parts) < 4:
            raise project_error(404, "NOT_FOUND")
        workbook = self.workbook(parts[3], namespace)
        if len(parts) == 5 and parts[4] == "history" and method == "GET":
            self.authorize(workbook, subject, write=False)
            return self.history(workbook)
        if len(parts) == 5 and parts[4] == "versions" and method == "POST":
            self.authorize(workbook, subject, write=True)
            return self.upload(request, subject, namespace, workbook)
        if len(parts) < 6 or parts[4] != "versions":
            raise project_error(404, "NOT_FOUND")
        version = self.versions.get(parts[5])
        if version is None or version.workbook_id != workbook.workbook_id:
            raise project_error(404, "NOT_FOUND")
        tail = parts[6:]
        if not tail and method == "GET":
            self.authorize(workbook, subject, write=False)
            return httpx.Response(200, json=self.version_body(version))
        if tail == ["source"] and method == "GET":
            self.authorize(workbook, subject, write=False)
            return self.binary(version.data, version.file_name)
        if tail == ["audits"] and method == "POST":
            self.authorize(workbook, subject, write=True)
            return self.reaudit(request, version, namespace)
        if tail == ["remediations"] and method == "POST":
            self.authorize(workbook, subject, write=True)
            return self.decisions(request, subject, namespace, workbook, version)
        if len(tail) >= 2 and tail[0] == "audits":
            audit = self.audits.get(tail[1])
            if audit is None or audit.version_id != version.version_id:
                raise project_error(404, "NOT_FOUND")
            self.authorize(workbook, subject, write=False)
            self.advance(audit)
            if len(tail) == 2 and method == "GET":
                return httpx.Response(200, json=self.audit_body(audit, version))
            if tail[2:] == ["report"] and method == "GET":
                if audit.status == "PROCESSING":
                    raise project_error(409, "REPORT_NOT_READY")
                assert audit.report is not None
                return httpx.Response(
                    200,
                    content=audit.report,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Disposition": (
                            f'attachment; filename="audit-{audit.audit_id}.json"'
                        ),
                        "Cache-Control": "no-store",
                        "X-Request-ID": "req-fake",
                    },
                )
            if tail[2:] == ["review"] and method == "GET":
                return self.review(audit, version, subject == workbook.owner)
        raise project_error(404, "NOT_FOUND")

    def workbook(self, workbook_id: str, namespace: str | None) -> Workbook:
        workbook = self.workbooks.get(workbook_id)
        if workbook is None or workbook.namespace != namespace:
            raise project_error(404, "NOT_FOUND")
        return workbook

    def authorize(self, workbook: Workbook, subject: str, *, write: bool) -> None:
        if subject == workbook.owner:
            return
        if not write and workbook.owner in self.reviewer_grants.get(subject, set()):
            return
        raise project_error(403, "FORBIDDEN")

    def binary(self, data: bytes, name: str) -> httpx.Response:
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
        return httpx.Response(
            200,
            content=data,
            headers={
                "Content-Type": XLSX,
                "Content-Disposition": (
                    f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
                ),
                "Cache-Control": "no-store",
                "Content-Length": str(len(data)),
                "X-Request-ID": "req-fake",
            },
        )

    # --- instructor interface ----------------------------------------------------------------

    def instructor(self, request: httpx.Request, path: str) -> httpx.Response:
        subject = self.subject(request)
        if subject is None:
            raise test_error(401, "valid test-admin credentials are required")
        if subject != "test-admin":
            raise test_error(403, "test-admin role required")
        if path == "/test/setup" and request.method == "POST":
            body = json.loads(request.content)
            session_id = body.get("session_id")
            setup = body.get("setup", {})
            if (
                not isinstance(session_id, str)
                or not ID.match(session_id)
                or not isinstance(setup, dict)
            ):
                raise test_error(400, "invalid setup request")
            if set(setup) - {"cleanup_session_ids", "faults"}:
                raise test_error(400, "unknown setup key")
            faults = setup.get("faults")
            if "faults" in setup:
                count = faults.get("audit_failures") if isinstance(faults, dict) else None
                if (
                    not isinstance(faults, dict)
                    or set(faults) != {"audit_failures"}
                    or type(count) is not int
                    or count != 1
                ):
                    raise test_error(400, "faults must be exactly {audit_failures: 1}")
                if not self.fault_switch:
                    raise test_error(403, "the trusted fault switch is disabled")
            if session_id in self.sessions:
                if self.duplicate_setup_extra_field:
                    return httpx.Response(
                        409, json={"message": "exists", "source": "system", "code": "DUP"}
                    )
                raise test_error(409, "session exists")
            for old in setup.get("cleanup_session_ids", []):
                self.cleanup(old)
            self.sessions[session_id] = {"faults": 1 if faults else 0}
            return httpx.Response(201, json={"session_id": session_id})
        if path == "/test/report" and request.method == "GET":
            session_id = parse_qs(urlsplit(str(request.url)).query).get("session_id", [""])[0]
            if session_id not in self.sessions:
                raise test_error(404, "unknown session")
            return httpx.Response(
                200, json={"session_id": session_id, "coverage": {"code": self.code_coverage}}
            )
        raise test_error(404, "unknown testing route")

    def cleanup(self, session_id: str) -> None:
        if session_id not in self.sessions:
            return
        del self.sessions[session_id]
        for workbook_id in [w for w, wb in self.workbooks.items() if wb.namespace == session_id]:
            for version_id in self.workbooks[workbook_id].versions:
                for audit_id in self.versions[version_id].audits:
                    self.audits.pop(audit_id, None)
                self.versions.pop(version_id, None)
            del self.workbooks[workbook_id]

    # --- uploads and audits ----------------------------------------------------------------

    def upload(
        self,
        request: httpx.Request,
        subject: str,
        namespace: str | None,
        workbook: Workbook | None,
    ) -> httpx.Response:
        content_type = request.headers.get("content-type", "").encode()
        message = BytesParser(policy=email_policy.HTTP).parsebytes(
            b"Content-Type: " + content_type + b"\r\n\r\n" + request.content
        )
        files = {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }
        part = files.get("file")
        if part is None or len(files) != 1:
            raise project_error(400, "INVALID_REQUEST")
        name = part.get_filename() or ""
        data = part.get_payload(decode=True)
        if name.endswith(".xlsm"):
            raise project_error(422, "MACRO_ENABLED_WORKBOOK_UNSUPPORTED")
        if not name.endswith(".xlsx"):
            raise project_error(415, "UNSUPPORTED_MEDIA_TYPE")
        sha = hashlib.sha256(data).hexdigest()
        fixture = self.expected.get(sha)
        # Unit tests shrink the byte limit for speed; committed fixtures stay acceptable.
        if len(data) > self.upload_limit and (fixture is None or len(data) > 25_000_000):
            raise project_error(413, "FILE_TOO_LARGE")
        if fixture is not None and fixture["kind"] == "rejected":
            raise project_error(fixture["http_status"], fixture.get("error_code") or "INVALID")
        if fixture is None:
            fixture = self.generated(data, self.worksheet_limit, self.cell_limit)
        if workbook is None:
            workbook = Workbook(self.new_id("wb"), subject, namespace)
            self.workbooks[workbook.workbook_id] = workbook
        version = Version(
            self.new_id("ver"), workbook.workbook_id, name, data, self.now(), None, expected=fixture
        )
        self.versions[version.version_id] = version
        workbook.versions.append(version.version_id)
        audit = self.start_audit(version, namespace)
        return httpx.Response(
            202,
            json={
                "workbookId": workbook.workbook_id,
                "versionId": version.version_id,
                "auditId": audit.audit_id,
                "status": "PROCESSING",
            },
        )

    @staticmethod
    def generated(data: bytes, worksheet_limit: int, cell_limit: int) -> dict[str, Any]:
        """Tester-generated boundary workbooks: intake rules only, then no findings."""
        try:
            package = zipfile.ZipFile(io.BytesIO(data))
            parts = {name: package.read(name) for name in package.namelist()}
        except zipfile.BadZipFile:
            raise project_error(422, "INVALID_WORKBOOK") from None
        workbook = parts.get("xl/workbook.xml", b"")
        sheets = [p for name, p in parts.items() if name.startswith("xl/worksheets/sheet")]
        if b"workbookProtection" in workbook or any(
            b"sheetProtection" in sheet and b'sheet="1"' in sheet for sheet in sheets
        ):
            raise project_error(422, "PROTECTED_WORKBOOK")
        if len(sheets) > worksheet_limit:
            raise project_error(422, "TOO_MANY_WORKSHEETS")
        if sum(sheet.count(b"<c r=") for sheet in sheets) > cell_limit:
            raise project_error(422, "TOO_MANY_CELLS")
        return {"findings": [], "limitation_facts": []}

    def active(self, version: Version) -> bool:
        if any(self.audits[a].status == "PROCESSING" for a in version.audits):
            return True
        return any(
            r.source_version_id == version.version_id and r.status == "REMEDIATING"
            for r in self.remediations.values()
        )

    def start_audit(self, version: Version, namespace: str | None, *, fault: bool = True) -> Audit:
        audit = Audit(
            self.new_id("aud"),
            version.version_id,
            "PROCESSING",
            self.now(),
            None,
            [],
            [],
            None,
            self.processing_polls,
        )
        session = self.sessions.get(namespace or "")
        audit_fault = bool(fault and session and session["faults"])
        if audit_fault and session is not None:
            session["faults"] = 0
        self.audits[audit.audit_id] = audit
        version.audits.append(audit.audit_id)
        audit.review = [{"fault": audit_fault or version.file_name in self.fail_audits_for}]
        if audit.polls_left == 0:
            self.finish(audit)
        return audit

    def findings_for(self, version: Version) -> tuple[list[dict[str, str]], list[str], Any]:
        sha = hashlib.sha256(version.data).hexdigest()
        if version.expected is not None:
            source = version.expected
            renames: dict[str, str] = {}
            removed: set[tuple[str, str]] = set()
        else:
            remediation = self.remediations[version.remediation_id or ""]
            parent = self.versions[remediation.source_version_id]
            source = parent.expected or {"findings": []}
            renames, removed = {}, set()
            for decision in remediation.decisions:
                if decision["action"] != "APPLY":
                    continue
                if not self.keep_applied_finding:
                    removed.add((decision["ruleId"], decision["target"]))
                if decision["ruleId"] == "WORKSHEET_NAME":
                    renames[decision["worksheet"]] = decision["value"]["name"]
        findings = []
        for item in source["findings"]:
            if (item["ruleId"], item["target"]) in removed:
                continue
            worksheet = renames.get(item["worksheet"], item["worksheet"])
            findings.append(
                {
                    "findingId": finding_id(
                        sha, RULESET, item["ruleId"], worksheet, item["target"]
                    ),
                    "ruleId": item["ruleId"],
                    "category": item["category"],
                    "worksheet": worksheet,
                    "target": item["target"],
                    "classification": item["classification"],
                    "message": f"{item['ruleId']} screening found an issue at {item['target']}.",
                    "recommendation": "Review the location and decide on a correction.",
                }
            )
        findings.sort(key=lambda f: (f["ruleId"], f["worksheet"], f["target"]))
        limitations = sorted(set(source.get("limitation_facts", [])))
        return findings, limitations, source

    def finish(self, audit: Audit) -> None:
        version = self.versions[audit.version_id]
        fault = audit.review and audit.review[0].get("fault")
        audit.completed = self.now()
        if fault:
            audit.status = "PROCESSING_FAILED"
            audit.error = {
                "code": "TEST_AUDIT_FAILURE",
                "message": "Audit failure requested by the isolated test session.",
                "requestId": f"job-{audit.audit_id}",
                "details": {"stage": "audit", "testOnly": True},
            }
            audit.review = []
        else:
            findings, limitations, source = self.findings_for(version)
            audit.findings, audit.limitations = findings, limitations
            if not findings:
                audit.status = "AUDIT_COMPLETE"
            elif any(f["classification"] == "REQUIRES_USER_INPUT" for f in findings):
                audit.status = "REMEDIATION_AVAILABLE"
            else:
                audit.status = "REVIEW_REQUIRED"
            audit.review = self.review_items(findings, source, version)
        version.stable_status = (
            "REMEDIATED"
            if version.source_version_id is not None and audit.status != "PROCESSING_FAILED"
            else audit.status
        )
        audit.report = self.build_report(audit, version)

    def advance(self, audit: Audit) -> None:
        if audit.status == "PROCESSING":
            if audit.polls_left > 0:
                audit.polls_left -= 1
            else:
                self.finish(audit)

    def audit_body(self, audit: Audit, version: Version) -> dict[str, Any]:
        return {
            "auditId": audit.audit_id,
            "workbookId": version.workbook_id,
            "versionId": version.version_id,
            "rulesetVersion": RULESET,
            "status": audit.status,
            "startedAt": audit.started,
            "completedAt": audit.completed,
            "findings": audit.findings,
            "limitations": audit.limitations,
            "error": audit.error,
        }

    def version_body(self, version: Version) -> dict[str, Any]:
        latest = self.audits[version.audits[-1]] if version.audits else None
        status = "PROCESSING" if latest and latest.status == "PROCESSING" else version.stable_status
        return {
            "workbookId": version.workbook_id,
            "versionId": version.version_id,
            "fileName": version.file_name,
            "sha256": hashlib.sha256(version.data).hexdigest(),
            "createdAt": version.created,
            "status": status,
            "latestAuditId": latest.audit_id if latest else None,
            "sourceVersionId": version.source_version_id,
        }

    def reaudit(
        self, request: httpx.Request, version: Version, namespace: str | None
    ) -> httpx.Response:
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                raise project_error(400, "INVALID_REQUEST") from None
            if not isinstance(body, dict) or body not in ({}, {"reason": "MANUAL_REAUDIT"}):
                raise project_error(400, "INVALID_REQUEST")
        if self.active(version):
            raise project_error(409, "ACTIVE_OPERATION")
        audit = self.start_audit(version, namespace)
        return httpx.Response(
            202,
            json={
                "auditId": audit.audit_id,
                "versionId": version.version_id,
                "rulesetVersion": RULESET,
                "status": "PROCESSING",
            },
        )

    def history(self, workbook: Workbook) -> httpx.Response:
        versions, audits = [], []
        for version_id in workbook.versions:
            version = self.versions[version_id]
            versions.append(
                {
                    "versionId": version_id,
                    "sourceVersionId": version.source_version_id,
                    "status": self.version_body(version)["status"],
                    "createdAt": version.created,
                }
            )
        ordered = sorted(
            (a for v in workbook.versions for a in self.versions[v].audits),
            key=lambda a: int(a.split("-")[1]),
        )
        for audit_id in ordered:
            audit = self.audits[audit_id]
            audits.append(
                {
                    "auditId": audit_id,
                    "versionId": audit.version_id,
                    "rulesetVersion": RULESET,
                    "status": audit.status,
                }
            )
        return httpx.Response(
            200, json={"workbookId": workbook.workbook_id, "versions": versions, "audits": audits}
        )

    # --- review context ------------------------------------------------------------------

    def review_items(
        self, findings: list[dict[str, str]], source: dict[str, Any], version: Version
    ) -> list[dict[str, Any]]:
        declared = {
            tuple(item["finding"]): item for item in source.get("extras", {}).get("review", [])
        }
        items = []
        for finding in findings:
            entry = declared.get((finding["ruleId"], finding["target"]))
            supported = finding["classification"] == "REQUIRES_USER_INPUT"
            items.append(
                {
                    "findingId": finding["findingId"],
                    "allowedActions": entry["allowedActions"]
                    if entry
                    else (
                        ["APPLY", "DECLINE", "MANUAL_REVIEW"]
                        if supported
                        else ["DECLINE", "MANUAL_REVIEW"]
                    ),
                    "currentValue": entry["currentValue"] if entry else None,
                    "reason": "Supported correction available."
                    if supported
                    else "Manual review only.",
                }
            )
        return items

    def review(self, audit: Audit, version: Version, owner: bool) -> httpx.Response:
        if audit.status in ("PROCESSING", "PROCESSING_FAILED"):
            raise project_error(409, "AUDIT_NOT_READY")
        if version.audits[-1] != audit.audit_id:
            raise project_error(409, "STALE_AUDIT")
        items = copy.deepcopy(audit.review)
        if not owner:
            for item in items:
                item["allowedActions"] = []
        return httpx.Response(
            200,
            json={
                "workbookId": version.workbook_id,
                "versionId": version.version_id,
                "auditId": audit.audit_id,
                "items": items,
            },
        )

    # --- reports -------------------------------------------------------------------------

    def build_report(self, audit: Audit, version: Version) -> bytes:
        remediation = (
            self.remediations.get(version.remediation_id) if version.remediation_id else None
        )
        decisions = remediation.decisions if remediation else []
        failed = audit.status == "PROCESSING_FAILED"
        body = {
            "reportVersion": "2.0.0",
            "specificationVersion": "2.1.0",
            "workbookId": version.workbook_id,
            "versionId": version.version_id,
            "auditId": audit.audit_id,
            "fileName": version.file_name,
            "sha256": hashlib.sha256(version.data).hexdigest(),
            "sourceVersionId": version.source_version_id,
            "auditDate": audit.completed,
            "rulesetVersion": RULESET,
            "status": audit.status,
            "checksPerformed": [] if failed else list(RULE_ORDER),
            "findings": audit.findings,
            "decisions": decisions,
            "approvedChanges": [d for d in decisions if d["action"] == "APPLY"],
            "unresolvedFindings": audit.findings,
            "manualReviewItems": [
                f for f in audit.findings if f["classification"] == "MANUAL_REVIEW"
            ],
            "limitations": audit.limitations,
            "remediationId": remediation.remediation_id if remediation else None,
            "outputVersionId": remediation.output_version_id if remediation else None,
            "outputId": remediation.output_id if remediation else None,
            "error": audit.error,
        }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    # --- decisions and remediation ------------------------------------------------------

    def decisions(
        self,
        request: httpx.Request,
        subject: str,
        namespace: str | None,
        workbook: Workbook,
        version: Version,
    ) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        if key is None or not ID.match(key):
            raise project_error(400, "IDEMPOTENCY_KEY_REQUIRED")
        try:
            body = json.loads(request.content)
        except json.JSONDecodeError:
            raise project_error(400, "INVALID_REQUEST") from None
        if not isinstance(body, dict) or set(body) != {"auditId", "decisions"}:
            raise project_error(400, "INVALID_REQUEST")
        decisions = body["decisions"]
        if not isinstance(body["auditId"], str) or not ID.match(body["auditId"]):
            raise project_error(400, "INVALID_REQUEST")
        if not isinstance(decisions, list) or not 1 <= len(decisions) <= 1000:
            raise project_error(400, "INVALID_REQUEST")
        for decision in decisions:
            if not isinstance(decision, dict) or not {"findingId", "action"} <= set(decision):
                raise project_error(400, "INVALID_REQUEST")
            if set(decision) - {"findingId", "action", "value"}:
                raise project_error(400, "INVALID_REQUEST")
            if not isinstance(decision["findingId"], str) or not SHA.match(decision["findingId"]):
                raise project_error(400, "INVALID_REQUEST")
            if decision["action"] not in ("APPLY", "DECLINE", "MANUAL_REVIEW"):
                raise project_error(400, "INVALID_REQUEST")
            if decision["action"] == "APPLY" and "value" not in decision:
                raise project_error(400, "MISSING_VALUE")
            if decision["action"] != "APPLY" and "value" in decision:
                raise project_error(400, "INVALID_REQUEST")
        ids = [d["findingId"] for d in decisions]
        if len(ids) != len(set(ids)):
            raise project_error(400, "DUPLICATE_DECISION")
        canonical = json.dumps(
            {
                "auditId": body["auditId"],
                "decisions": sorted(decisions, key=lambda d: d["findingId"]),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        scope = (subject, namespace, request.url.path, key)
        if scope in self.keys:
            digest, response = self.keys[scope]
            if digest != canonical:
                raise project_error(409, "IDEMPOTENCY_CONFLICT")
            return httpx.Response(202, json=response)
        if self.active(version):
            raise project_error(409, "ACTIVE_OPERATION")
        latest = self.audits[version.audits[-1]]
        if body["auditId"] != latest.audit_id:
            raise project_error(409, "STALE_AUDIT")
        if latest.status in ("PROCESSING", "PROCESSING_FAILED"):
            raise project_error(409, "AUDIT_NOT_READY")
        by_id = {f["findingId"]: f for f in latest.findings}
        names: list[str] = []
        records = []
        for decision in decisions:
            finding = by_id.get(decision["findingId"])
            if finding is None:
                raise project_error(404, "FINDING_NOT_FOUND")
            if decision["action"] == "APPLY":
                self.validate_apply(finding, decision["value"], version)
                if finding["ruleId"] == "WORKSHEET_NAME":
                    names.append(decision["value"]["name"].casefold())
            records.append((finding, decision))
        if len(names) != len(set(names)):
            raise project_error(422, "CONFLICTING_CORRECTIONS")
        order = {f["findingId"]: i for i, f in enumerate(latest.findings)}
        records.sort(key=lambda pair: order[pair[0]["findingId"]])
        remediation = Remediation(
            self.new_id("rem"),
            workbook.workbook_id,
            version.version_id,
            latest.audit_id,
            [
                {
                    "sourceAuditId": latest.audit_id,
                    "findingId": f["findingId"],
                    "ruleId": f["ruleId"],
                    "worksheet": f["worksheet"],
                    "target": f["target"],
                    "action": d["action"],
                    "value": d.get("value"),
                }
                for f, d in records
            ],
            self.remediation_polls,
        )
        self.remediations[remediation.remediation_id] = remediation
        accepted = {
            "remediationId": remediation.remediation_id,
            "sourceVersionId": version.version_id,
            "status": "REMEDIATING",
        }
        self.keys[scope] = (canonical, accepted)
        return httpx.Response(202, json=accepted)

    def validate_apply(self, finding: dict[str, str], value: Any, version: Version) -> None:
        if finding["classification"] != "REQUIRES_USER_INPUT" and not self.accept_manual_apply:
            raise project_error(422, "UNSUPPORTED_CORRECTION")
        rule = finding["ruleId"]
        shapes = {
            "ALT_TEXT": {"title", "description"},
            "WORKSHEET_NAME": {"name"},
            "LINK_TEXT": {"text"},
            "TABLE_HEADER": {"headerRow"},
        }
        if not isinstance(value, dict) or set(value) != shapes.get(rule, set()):
            raise project_error(422, "INVALID_CORRECTION")
        if rule == "ALT_TEXT":
            title, description = value["title"], value["description"]
            if not isinstance(title, str) or not isinstance(description, str):
                raise project_error(422, "INVALID_CORRECTION")
            if not title.strip() or not description.strip():
                raise project_error(422, "INVALID_CORRECTION")
            if len(title) > 200 or len(description) > 800:
                raise project_error(422, "INVALID_CORRECTION")
        elif rule == "LINK_TEXT":
            text = value["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                raise project_error(422, "INVALID_CORRECTION")
            if is_ambiguous_label(text):
                raise project_error(422, "INVALID_CORRECTION")
        elif rule == "TABLE_HEADER":
            row = value["headerRow"]
            expected = 1
            if type(row) is not int or row != expected:
                raise project_error(422, "INVALID_CORRECTION")
        elif rule == "WORKSHEET_NAME":
            name = value["name"]
            existing = re.findall(
                r'<sheet name="([^"]*)"', self.part(version.data, "xl/workbook.xml")
            )
            current = finding["worksheet"]
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 31
                or name != name.strip()
                or FORBIDDEN_NAME.search(name)
                or name.startswith("'")
                or name.endswith("'")
                or name.casefold() == "history"
                or is_generic_sheet_name(name)
                or name.casefold() == current.casefold()
                or any(
                    name.casefold() == other.casefold() for other in existing if other != current
                )
            ):
                raise project_error(422, "INVALID_CORRECTION")

    @staticmethod
    def part(data: bytes, name: str) -> str:
        return zipfile.ZipFile(io.BytesIO(data)).read(name).decode("utf-8")

    def remediation_status(
        self, remediation_id: str, subject: str, namespace: str | None
    ) -> httpx.Response:
        remediation = self.remediations.get(remediation_id)
        if remediation is None:
            raise project_error(404, "NOT_FOUND")
        workbook = self.workbook(remediation.workbook_id, namespace)
        self.authorize(workbook, subject, write=False)
        if remediation.status == "REMEDIATING":
            if remediation.polls_left > 0:
                remediation.polls_left -= 1
            else:
                self.complete(remediation, workbook)
        return httpx.Response(
            200,
            json={
                "remediationId": remediation.remediation_id,
                "status": remediation.status,
                "sourceVersionId": remediation.source_version_id,
                "outputVersionId": remediation.output_version_id,
                "outputId": remediation.output_id,
                "auditId": remediation.audit_id,
                "error": remediation.error,
            },
        )

    def complete(self, remediation: Remediation, workbook: Workbook) -> None:
        source = self.versions[remediation.source_version_id]
        data = self.edit(source.data, remediation.decisions)
        base = source.file_name.removesuffix(".xlsx")
        output = Version(
            self.new_id("ver"),
            workbook.workbook_id,
            f"{base}-remediated.xlsx",
            data,
            self.now(),
            source.version_id,
            remediation.remediation_id,
        )
        remediation.output_version_id = output.version_id
        remediation.output_id = self.new_id("out")
        self.versions[output.version_id] = output
        workbook.versions.append(output.version_id)
        self.outputs[remediation.output_id] = output.version_id
        audit = self.start_audit(output, None, fault=False)
        self.finish(audit)
        remediation.audit_id = audit.audit_id
        remediation.status = "REMEDIATED"

    def output_download(
        self, output_id: str, subject: str, namespace: str | None
    ) -> httpx.Response:
        version_id = self.outputs.get(output_id)
        if version_id is None:
            raise project_error(404, "NOT_FOUND")
        version = self.versions[version_id]
        workbook = self.workbook(version.workbook_id, namespace)
        self.authorize(workbook, subject, write=False)
        return self.binary(version.data, version.file_name)

    # --- editor (only understands Team 9 synthetic fixture XML) --------------------------

    def edit(self, data: bytes, decisions: list[dict[str, Any]]) -> bytes:
        source = zipfile.ZipFile(io.BytesIO(data))
        parts = {info.filename: source.read(info.filename) for info in source.infolist()}
        order = [info.filename for info in source.infolist()]
        applied = [d for d in decisions if d["action"] == "APPLY"]
        if not applied and self.mutate_output is None:
            return data
        families = ["ALT_TEXT", "LINK_TEXT", "TABLE_HEADER", "WORKSHEET_NAME"]
        for family in families:
            for decision in [d for d in applied if d["ruleId"] == family]:
                part, _, locator = decision["target"].partition("#")
                getattr(self, f"_edit_{family.lower()}")(parts, part, locator, decision)
        if self.mutate_output is not None:
            self.mutate_output(parts)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in order + sorted(set(parts) - set(order)):
                archive.writestr(name, parts[name])
        output = stream.getvalue()
        if self.corrupt_output:
            # Damage compressed entry data in place so the central directory still parses.
            middle = len(output) // 3
            output = (
                output[:middle]
                + bytes(b ^ 0xFF for b in output[middle : middle + 64])
                + (output[middle + 64 :])
            )
        return output

    @staticmethod
    def _attr(value: str) -> str:
        return escape(value, {'"': "&quot;", "\n": "&#10;", "\t": "&#9;", "\r": "&#13;"})

    def _edit_alt_text(
        self, parts: dict[str, bytes], part: str, locator: str, decision: dict[str, Any]
    ) -> None:
        object_id = locator.removeprefix("id=")
        xml = parts[part].decode()
        pattern = re.compile(rf'<xdr:cNvPr id="{object_id}"([^>]*?)(/?)>')
        match = pattern.search(xml)
        assert match is not None
        attrs = re.sub(r'\s(title|descr)="[^"]*"', "", match.group(1))
        value = decision["value"]
        new = (
            f'<xdr:cNvPr id="{object_id}"{attrs} title="{self._attr(value["title"])}" '
            f'descr="{self._attr(value["description"])}"{match.group(2)}>'
        )
        parts[part] = (xml[: match.start()] + new + xml[match.end() :]).encode()

    def _edit_link_text(
        self, parts: dict[str, bytes], part: str, locator: str, decision: dict[str, Any]
    ) -> None:
        ref = locator.removeprefix("cell=")
        xml = parts[part].decode()
        match = re.search(rf'<c r="{ref}"( s="\d+")?[^>]*?(/>|>.*?</c>)', xml)
        assert match is not None
        style = match.group(1) or ""
        text = escape(decision["value"]["text"])
        new = f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
        parts[part] = (xml[: match.start()] + new + xml[match.end() :]).encode()

    def _edit_table_header(
        self, parts: dict[str, bytes], part: str, locator: str, decision: dict[str, Any]
    ) -> None:
        if locator.startswith("tableId="):
            xml = parts[part].decode().replace('headerRowCount="0"', 'headerRowCount="1"', 1)
            parts[part] = xml.encode()
            return
        rectangle = locator.removeprefix("range=")
        sheet = parts[part].decode()
        first, last = rectangle.split(":")
        columns = []
        first_row = re.search(r'<row r="1">(.*?)</row>', sheet)
        assert first_row is not None
        for label in re.findall(r"<t[^>]*>(.*?)</t>", first_row.group(1)):
            columns.append(unescape(label))
        width = ord(last[0]) - ord(first[0]) + 1
        columns = columns[:width]
        rels_name = part.replace("worksheets/", "worksheets/_rels/") + ".rels"
        rels = parts.get(rels_name, b"").decode()
        used = {int(n) for n in re.findall(r'Id="rId(\d+)"', rels)}
        rel_number = next(n for n in itertools.count(1) if n not in used)
        tables = [n for n in parts if n.startswith("xl/tables/table")]
        table_number = next(
            n for n in itertools.count(1) if f"xl/tables/table{n}.xml" not in tables
        )
        name = f"A11yTable{table_number}"
        cols = "".join(
            f'<tableColumn id="{i}" name="{escape(c)}"/>' for i, c in enumerate(columns, 1)
        )
        parts[f"xl/tables/table{table_number}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'id="{table_number}" name="{name}" displayName="{name}" ref="{rectangle}" '
            f'headerRowCount="1" totalsRowShown="0"><tableColumns count="{len(columns)}">'
            f"{cols}</tableColumns></table>"
        ).encode()
        relationship = (
            f'<Relationship Id="rId{rel_number}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/table" Target="../tables/table{table_number}.xml"/>'
        )
        if rels:
            rels = rels.replace("</Relationships>", relationship + "</Relationships>")
        else:
            rels = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                f'relationships">{relationship}</Relationships>'
            )
        parts[rels_name] = rels.encode()
        parts[part] = sheet.replace(
            "</worksheet>",
            f'<tableParts count="1"><tablePart r:id="rId{rel_number}"/></tableParts></worksheet>',
        ).encode()
        types = (
            parts["[Content_Types].xml"]
            .decode()
            .replace(
                "</Types>",
                f'<Override PartName="/xl/tables/table{table_number}.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/></Types>',
            )
        )
        parts["[Content_Types].xml"] = types.encode()

    def _edit_worksheet_name(
        self, parts: dict[str, bytes], part: str, locator: str, decision: dict[str, Any]
    ) -> None:
        old, new = decision["worksheet"], decision["value"]["name"]
        quoted = "'" + new.replace("'", "''") + "'"
        workbook = parts["xl/workbook.xml"].decode()
        workbook = workbook.replace(
            f'<sheet name="{escape(old)}"', f'<sheet name="{escape(new)}"', 1
        )
        parts["xl/workbook.xml"] = workbook.encode()

        def rewrite(formula: str) -> str:
            pieces = re.split(r'("(?:[^"]|"")*")', formula)
            for index in range(0, len(pieces), 2):
                text = pieces[index]
                text = text.replace(f"'{old}'!", f"{quoted}!")
                text = re.sub(rf"(?<![A-Za-z0-9_.'\]]){re.escape(old)}!", f"{quoted}!", text)
                pieces[index] = text
            return "".join(pieces)

        def element(match: re.Match[str]) -> str:
            return match.group(1) + escape(rewrite(unescape(match.group(2)))) + match.group(3)

        def attribute(match: re.Match[str]) -> str:
            value = unescape(match.group(2), {"&quot;": '"'})
            return match.group(1) + self._attr(rewrite(value)) + match.group(3)

        for name in list(parts):
            if not name.endswith(".xml") or name.startswith("docProps/"):
                continue
            xml = parts[name].decode()
            updated = re.sub(
                r"(<(?:f|c:f|xm:f|definedName|formula1|formula2|formula)(?:\s[^>]*)?>)(.*?)(</)",
                element,
                xml,
            )
            updated = re.sub(r'(location=")([^"]*)(")', attribute, updated)
            if updated != xml:
                parts[name] = updated.encode()


def category(rule: str) -> str:
    return CATEGORY[rule]
