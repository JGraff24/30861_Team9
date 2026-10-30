"""Black-box Phase 4 checks (specification 2.1.0). Each returns a pass message or raises a signal.

Oracles are independent of the system: wire shapes come from the frozen JSON Schema pack,
expected findings from hand-written fixture expectations plus the tester's own finding-ID
function, and output workbooks from the tester's package comparator. Handlers share uploaded
resources through `ctx.state` and skip (never pass) when a resource they need does not exist.
"""

import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from tester.phase4 import contract
from tester.phase4.package import PackageError, compare_packages
from tester.phase4.policy import RULE_ORDER
from tester.suite.checks import (
    XLSX,
    CheckFailed,
    Prerequisite,
    SuiteContext,
    expect_status,
)
from tester.suite.identity import finding_id
from tester.suite.manifest import AcceptedFixture, SuiteConfigurationError
from tester.target import DeadlineExceeded, Identity

Json = Any
SessionChoice = str | Literal["main", "none"]
INVALID_TOKEN = "team9-invalid-negative-test-token"
POLL_SECONDS = 1.0
FAULT_MESSAGE = "Audit failure requested by the isolated test session."
FAULT_DETAILS = {"stage": "audit", "testOnly": True}
TERMINAL_AUDIT = {"AUDIT_COMPLETE", "REVIEW_REQUIRED", "REMEDIATION_AVAILABLE", "PROCESSING_FAILED"}


# --- limits and requests ---------------------------------------------------------------------


@contextmanager
def setup(ctx: SuiteContext) -> Iterator[None]:
    """Shared or preparatory calls: they never record coverage markers (section 16)."""
    ctx.setup_depth += 1
    try:
        yield
    finally:
        ctx.setup_depth -= 1


def limit(ctx: SuiteContext, name: str, default: int) -> int:
    limits = ctx.suite.manifest.limits
    return int(getattr(limits, name)) if limits is not None else default


def call(
    ctx: SuiteContext,
    method: Literal["GET", "POST"],
    path: str,
    *,
    identity: Identity | None = "userA",
    raw_token: str | None = None,
    session: SessionChoice = "main",
    binary: bool = False,
    timeout: float | None = None,
    headers: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """One project call. `session` is "main", "none" (ordinary namespace) or a session ID."""
    extra = dict(headers or {})
    if session not in ("main", "none"):
        extra["X-Test-Session"] = session
    if timeout is None:
        timeout = (
            limit(ctx, "business_get_seconds", 10)
            if method == "GET"
            else limit(ctx, "upload_seconds", 35)
        )
    max_bytes = (
        limit(ctx, "binary_response_bytes", 33_554_432)
        if binary
        else limit(ctx, "json_response_bytes", 67_108_864)
    )
    return ctx.client.request(
        method,
        path,
        identity=identity,
        raw_token=raw_token,
        session_header=session == "main",
        headers=extra,
        timeout=timeout,
        max_bytes=max_bytes,
        **kwargs,
    )


def body_of(response: httpx.Response, definition: str, action: str) -> Json:
    try:
        return contract.parse_json(response.content, definition)
    except contract.ContractViolation as exc:
        raise CheckFailed(f"{action}: {exc}.") from None


def ok(
    ctx: SuiteContext, response: httpx.Response, status: int, definition: str, action: str
) -> Json:
    if response.status_code >= 500 and status < 500:
        expect_status(response, status, action)
    ctx.qualify()
    expect_status(response, status, action)
    return body_of(response, definition, action)


def project_error(
    ctx: SuiteContext,
    response: httpx.Response,
    status: int,
    action: str,
    code: str | None = None,
) -> Json:
    if response.status_code >= 500 and status < 500:
        expect_status(response, status, action)
    ctx.qualify()
    expect_status(response, status, action)
    error = body_of(response, "ProjectError", action)
    if code is not None and error["code"] != code:
        raise CheckFailed(f"{action} returned code {bounded(error['code'], 64)}; expected {code}.")
    if not response.headers.get("x-request-id"):
        raise CheckFailed(f"{action} response lacks X-Request-ID.")
    return error


def testing_error(
    ctx: SuiteContext, response: httpx.Response, status: int, action: str, source: str | None
) -> Json:
    ctx.qualify()
    expect_status(response, status, action)
    error = body_of(response, "TestEndpointError", action)
    if source is not None and error["source"] != source:
        raise CheckFailed(f"{action} reported source {error['source']}; expected {source}.")
    return error


# --- paths -------------------------------------------------------------------------------------


def version_path(ids: Mapping[str, str], version_id: str | None = None) -> str:
    return f"/api/v1/workbooks/{ids['workbookId']}/versions/{version_id or ids['versionId']}"


def audit_path(
    ids: Mapping[str, str], audit_id: str | None = None, version_id: str | None = None
) -> str:
    return f"{version_path(ids, version_id)}/audits/{audit_id or ids['auditId']}"


def history_path(ids: Mapping[str, str]) -> str:
    return f"/api/v1/workbooks/{ids['workbookId']}/history"


# --- uploads, audits and fixtures ---------------------------------------------------------------


def fresh(ctx: SuiteContext, values: list[str], action: str) -> None:
    seen: set[str] = ctx.state.setdefault("accepted_resource_ids", set())
    if len(set(values)) != len(values) or set(values) & seen:
        raise CheckFailed(f"{action} did not allocate distinct fresh resource IDs.")
    seen.update(values)


def multipart(data: bytes, name: str) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, XLSX)}


def upload(
    ctx: SuiteContext,
    fixture: str,
    action: str,
    *,
    session: SessionChoice = "main",
    data: bytes | None = None,
    name: str | None = None,
) -> dict[str, str]:
    accepted = ctx.suite.accepted(fixture)
    content = ctx.suite.bytes_of(fixture) if data is None else data
    response = call(
        ctx,
        "POST",
        "/api/v1/workbooks",
        session=session,
        files=multipart(content, name or accepted.upload_name),
    )
    ids: dict[str, str] = ok(ctx, response, 202, "UploadResponse", action)
    fresh(ctx, [ids["workbookId"], ids["versionId"], ids["auditId"]], action)
    return ids


def poll_audit(
    ctx: SuiteContext,
    ids: Mapping[str, str],
    action: str,
    *,
    audit_id: str | None = None,
    version_id: str | None = None,
    session: SessionChoice = "main",
    allow_failure: bool = False,
) -> Json:
    """Poll about once a second within the section 10 audit deadline (no extension per poll)."""
    seconds = limit(ctx, "audit_poll_seconds", 80)
    give_up = ctx.clock() + seconds
    wanted = audit_id or ids["auditId"]
    while True:
        left = give_up - ctx.clock()
        if left <= 0:
            raise CheckFailed(f"{action} was still PROCESSING after {seconds} s.")
        response = call(
            ctx,
            "GET",
            audit_path(ids, wanted, version_id),
            session=session,
            timeout=min(limit(ctx, "business_get_seconds", 10), left),
        )
        expect_status(response, 200, action)
        audit = body_of(response, "AuditResponse", action)
        if (audit["auditId"], audit["workbookId"]) != (wanted, ids["workbookId"]) or audit[
            "versionId"
        ] != (version_id or ids["versionId"]):
            raise CheckFailed(f"{action} response identifiers differ from the requested audit.")
        if audit["rulesetVersion"] != ctx.suite.manifest.ruleset_version:
            raise CheckFailed(f"{action} reports ruleset {audit['rulesetVersion']}.")
        if audit["status"] == "PROCESSING_FAILED" and not allow_failure:
            # Section 1: PROCESSING_FAILED is not an acceptable result for an accepted fixture.
            code = bounded(str(audit["error"]["code"]), 64)
            raise CheckFailed(f"{action} ended PROCESSING_FAILED ({code}).")
        if audit["status"] != "PROCESSING":
            started = contract.parse_timestamp(audit["startedAt"])
            if contract.parse_timestamp(audit["completedAt"]) < started:
                raise CheckFailed(f"{action} completed before it started.")
            return audit
        if ctx.client.remaining() < POLL_SECONDS:
            raise DeadlineExceeded("suite deadline reached while polling an audit")
        if ctx.clock() + POLL_SECONDS > give_up:
            raise CheckFailed(f"{action} was still PROCESSING after {seconds} s.")
        ctx.sleep(POLL_SECONDS)


def bounded(text: str, limit: int = 120) -> str:
    """Keep target-supplied text (sheet names, codes, targets) short in report messages."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def shared_failure(ctx: SuiteContext, cache: str) -> None:
    reason = ctx.state.get(f"{cache}:failed")
    if reason is not None:
        raise Prerequisite(f"Not executed: a shared resource failed earlier ({reason})")


@contextmanager
def remember_failure(ctx: SuiteContext, cache: str) -> Iterator[None]:
    """Record why a shared upload/audit/remediation failed so dependents skip quickly."""
    try:
        yield
    except DeadlineExceeded:
        raise
    except Exception as exc:
        ctx.state[f"{cache}:failed"] = bounded(str(exc) or type(exc).__name__, 160)
        raise


def audited(ctx: SuiteContext, fixture: str, key: str) -> tuple[dict[str, str], Json]:
    """Upload a fixture once per key and wait for its successful audit (cached for the run)."""
    cache = f"p4:{key}"
    shared_failure(ctx, cache)
    if cache not in ctx.state:
        with remember_failure(ctx, cache), setup(ctx):
            ids = upload(ctx, fixture, f"{fixture} upload")
            audit = poll_audit(ctx, ids, f"{fixture} audit")
        ctx.state[cache] = (ids, audit)
    ids, audit = ctx.state[cache]
    return dict(ids), copy.deepcopy(audit)


def source_sha(ctx: SuiteContext, fixture: str) -> str:
    return ctx.suite.accepted(fixture).sha256


Tuple6 = tuple[str, str, str, str, str, str]


def expected_tuples(
    ctx: SuiteContext,
    fixture: AcceptedFixture,
    sha: str,
    *,
    renames: Mapping[str, str] | None = None,
    removed: set[tuple[str, str]] | None = None,
) -> list[Tuple6]:
    ruleset = ctx.suite.manifest.ruleset_version
    rows = []
    for finding in fixture.findings:
        if removed and (finding.ruleId, finding.target) in removed:
            continue
        worksheet = (renames or {}).get(finding.worksheet, finding.worksheet)
        rows.append(
            (
                finding_id(sha, ruleset, finding.ruleId, worksheet, finding.target),
                finding.ruleId,
                str(finding.category),
                worksheet,
                finding.target,
                str(finding.classification),
            )
        )
    return sorted(rows, key=lambda row: (row[1], row[3], row[4]))


def actual_tuples(audit: Json) -> list[Tuple6]:
    return [
        (
            f["findingId"],
            f["ruleId"],
            f["category"],
            f["worksheet"],
            f["target"],
            f["classification"],
        )
        for f in audit["findings"]
    ]


def describe(actual: list[Tuple6], expected: list[Tuple6]) -> str:
    missing = [row for row in expected if row not in actual]
    unexpected = [row for row in actual if row not in expected]
    if missing or unexpected:
        sample = (missing or unexpected)[0]
        label = "missing" if missing else "unexpected"
        return (
            f"{len(missing)} expected finding(s) missing and {len(unexpected)} unexpected; first "
            f"{label}: {sample[1]} {bounded(repr(sample[3]), 60)} {bounded(sample[4])} "
            f"{sample[5]}."
        )
    return "Findings match as a set but are not in (ruleId, worksheet, target) order."


def status_for(rows: list[Tuple6]) -> str:
    if not rows:
        return "AUDIT_COMPLETE"
    if any(row[5] in ("REQUIRES_USER_INPUT", "AUTOMATICALLY_REMEDIABLE") for row in rows):
        return "REMEDIATION_AVAILABLE"
    return "REVIEW_REQUIRED"


def check_limitations(audit: Json, fixture: AcceptedFixture, action: str) -> None:
    limitations = audit["limitations"]
    if limitations != sorted(set(limitations)):
        raise CheckFailed(f"{action} limitations are not deduplicated and code-point sorted.")
    if len(limitations) < fixture.min_limitations:
        raise CheckFailed(
            f"{action} disclosed {len(limitations)} limitation(s); the fixture requires at least "
            f"{fixture.min_limitations} ({'; '.join(fixture.limitation_facts)})."
        )


def check_audit(
    ctx: SuiteContext, audit: Json, fixture_name: str, action: str, *, sha: str | None = None
) -> list[Tuple6]:
    fixture = ctx.suite.accepted(fixture_name)
    expected = expected_tuples(ctx, fixture, sha or fixture.sha256)
    if audit["status"] != fixture.expected_status:
        raise CheckFailed(f"{action} ended {audit['status']}; expected {fixture.expected_status}.")
    actual = actual_tuples(audit)
    if actual != expected:
        raise CheckFailed(f"{action}: {describe(actual, expected)}")
    check_limitations(audit, fixture, action)
    return expected


def finding_lookup(
    ctx: SuiteContext, fixture: str, sha: str | None = None
) -> dict[tuple[str, str], str]:
    accepted = ctx.suite.accepted(fixture)
    ruleset = ctx.suite.manifest.ruleset_version
    lookup = {
        (f.ruleId, f.target): finding_id(
            sha or accepted.sha256, ruleset, f.ruleId, f.worksheet, f.target
        )
        for f in accepted.findings
    }
    if len(lookup) != len(accepted.findings):
        raise SuiteConfigurationError(f"fixture {fixture} repeats a (ruleId, target) pair")
    return lookup


def extras(ctx: SuiteContext, fixture: str, key: str) -> Any:
    value = ctx.suite.accepted(fixture).extras.get(key)
    if value is None:
        raise Prerequisite(f"Not executed: fixture {fixture} declares no {key} data.")
    return value


# --- downloads ------------------------------------------------------------------------------


def download(
    ctx: SuiteContext,
    path: str,
    action: str,
    *,
    identity: Identity = "userA",
    session: SessionChoice = "main",
) -> tuple[bytes, httpx.Headers]:
    response = call(
        ctx,
        "GET",
        path,
        identity=identity,
        session=session,
        binary=True,
        timeout=limit(ctx, "download_seconds", 30),
    )
    if response.status_code >= 500:
        expect_status(response, 200, action)
    ctx.qualify()
    expect_status(response, 200, action)
    headers = response.headers
    media = headers.get("content-type", "").split(";")[0].strip()
    if media != XLSX:
        raise CheckFailed(f"{action} media type is {media or 'missing'}.")
    length = headers.get("content-length")
    if length is None or not length.isdigit() or int(length) != len(response.content):
        raise CheckFailed(f"{action} lacks a Content-Length matching the body.")
    if "attachment" not in headers.get("content-disposition", "").lower():
        raise CheckFailed(f"{action} is not served as an attachment.")
    if "no-store" not in headers.get("cache-control", "").lower():
        raise CheckFailed(f"{action} lacks Cache-Control: no-store.")
    return response.content, headers


def report_bytes(
    ctx: SuiteContext,
    ids: Mapping[str, str],
    audit_id: str,
    action: str,
    *,
    version_id: str | None = None,
    session: SessionChoice = "main",
    identity: Identity = "userA",
) -> tuple[bytes, Json]:
    response = call(
        ctx,
        "GET",
        f"{audit_path(ids, audit_id, version_id)}/report",
        identity=identity,
        session=session,
        timeout=limit(ctx, "download_seconds", 30),
    )
    report = ok(ctx, response, 200, "AuditReport", action)
    if response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise CheckFailed(f"{action} is not application/json.")
    disposition = response.headers.get("content-disposition", "")
    if "attachment" not in disposition.lower() or f"audit-{audit_id}.json" not in disposition:
        raise CheckFailed(f"{action} is not the attachment audit-{audit_id}.json.")
    if "no-store" not in response.headers.get("cache-control", "").lower():
        raise CheckFailed(f"{action} lacks Cache-Control: no-store.")
    return response.content, report


def history(
    ctx: SuiteContext, ids: Mapping[str, str], action: str, *, session: SessionChoice = "main"
) -> Json:
    response = call(ctx, "GET", history_path(ids), session=session)
    expect_status(response, 200, action)
    body = body_of(response, "HistoryResponse", action)
    if body["workbookId"] != ids["workbookId"]:
        raise CheckFailed(f"{action} names a different workbook.")
    return body


def version(
    ctx: SuiteContext,
    ids: Mapping[str, str],
    action: str,
    *,
    version_id: str | None = None,
    session: SessionChoice = "main",
) -> Json:
    response = call(ctx, "GET", version_path(ids, version_id), session=session)
    expect_status(response, 200, action)
    return body_of(response, "VersionResponse", action)


# --- decisions and remediation ---------------------------------------------------------------


def decision_body(
    ctx: SuiteContext, fixture: str, audit_id: str, names: list[str], *, sha: str | None = None
) -> Json:
    table = extras(ctx, fixture, "decisions")
    lookup = finding_lookup(ctx, fixture, sha)
    decisions = []
    for name in names:
        item = table[name]
        decision: dict[str, Any] = {
            "findingId": lookup[(item["finding"][0], item["finding"][1])],
            "action": item["action"],
        }
        if "value" in item:
            decision["value"] = item["value"]
        decisions.append(decision)
    return {"auditId": audit_id, "decisions": decisions}


def new_key(prefix: str = "t9") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def submit(
    ctx: SuiteContext,
    ids: Mapping[str, str],
    body: Json,
    *,
    key: str | None,
    identity: Identity | None = "userA",
    raw_token: str | None = None,
    session: SessionChoice = "main",
    raw: bytes | None = None,
) -> httpx.Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    if raw is not None:
        headers["Content-Type"] = "application/json"
        return call(
            ctx,
            "POST",
            f"{version_path(ids)}/remediations",
            identity=identity,
            raw_token=raw_token,
            session=session,
            headers=headers,
            content=raw,
        )
    return call(
        ctx,
        "POST",
        f"{version_path(ids)}/remediations",
        identity=identity,
        raw_token=raw_token,
        session=session,
        headers=headers,
        json=body,
    )


def poll_remediation(ctx: SuiteContext, remediation_id: str, action: str) -> Json:
    seconds = limit(ctx, "remediation_poll_seconds", 210)
    give_up = ctx.clock() + seconds
    while True:
        left = give_up - ctx.clock()
        if left <= 0:
            raise CheckFailed(f"{action} was still REMEDIATING after {seconds} s.")
        response = call(
            ctx,
            "GET",
            f"/api/v1/remediations/{remediation_id}",
            timeout=min(limit(ctx, "business_get_seconds", 10), left),
        )
        expect_status(response, 200, action)
        status = body_of(response, "RemediationResponse", action)
        if status["remediationId"] != remediation_id:
            raise CheckFailed(f"{action} names a different remediation.")
        if status["status"] != "REMEDIATING":
            return status
        if ctx.client.remaining() < POLL_SECONDS:
            raise DeadlineExceeded("suite deadline reached while polling a remediation")
        if ctx.clock() + POLL_SECONDS > give_up:
            raise CheckFailed(f"{action} was still REMEDIATING after {seconds} s.")
        ctx.sleep(POLL_SECONDS)


def plan_of(ctx: SuiteContext, fixture: str, plan: str) -> Json:
    plans = extras(ctx, fixture, "plans")
    if plan not in plans:
        raise Prerequisite(f"Not executed: fixture {fixture} has no {plan} plan.")
    return plans[plan]


def run_plan(ctx: SuiteContext, fixture: str, plan: str, key: str) -> Json:
    """Upload, audit, submit a named plan and wait for REMEDIATED; cached per key."""
    cache = f"p4:remediation:{key}"
    shared_failure(ctx, cache)
    if cache in ctx.state:
        return ctx.state[cache]
    with remember_failure(ctx, cache), setup(ctx):
        result = _run_plan(ctx, fixture, plan, key)
    ctx.state[cache] = result
    return result


def _run_plan(ctx: SuiteContext, fixture: str, plan: str, key: str) -> Json:
    ids, audit = audited(ctx, fixture, key)
    source_report, _ = report_bytes(ctx, ids, audit["auditId"], f"{fixture} source report")
    details = plan_of(ctx, fixture, plan)
    body = decision_body(ctx, fixture, audit["auditId"], details["decisions"])
    accepted = ok(
        ctx,
        submit(ctx, ids, body, key=new_key()),
        202,
        "RemediationAccepted",
        f"{fixture} {plan} decisions",
    )
    if accepted["sourceVersionId"] != ids["versionId"]:
        raise CheckFailed("RemediationAccepted names a different source version.")
    fresh(ctx, [accepted["remediationId"]], f"{fixture} {plan} decisions")
    status = poll_remediation(ctx, accepted["remediationId"], f"{fixture} {plan} remediation")
    if status["status"] != "REMEDIATED":
        error = status["error"] or {}
        raise CheckFailed(
            f"{fixture} {plan} remediation ended {status['status']} ({error.get('code')})."
        )
    output, headers = download(
        ctx, f"/api/v1/outputs/{status['outputId']}/workbook", f"{fixture} {plan} output download"
    )
    result = {
        "fixture": fixture,
        "plan": plan,
        "ids": ids,
        "audit": audit,
        "source_report": source_report,
        "body": body,
        "accepted": accepted,
        "status": status,
        "output": output,
        "output_headers": dict(headers),
        "output_sha": hashlib.sha256(output).hexdigest(),
    }
    return result


def expected_output(ctx: SuiteContext, result: Json) -> list[Tuple6]:
    fixture = ctx.suite.accepted(result["fixture"])
    details = plan_of(ctx, result["fixture"], result["plan"])
    decisions = extras(ctx, result["fixture"], "decisions")
    removed = set()
    renames: dict[str, str] = {}
    for name in details["decisions"]:
        item = decisions[name]
        if item["action"] == "APPLY":
            removed.add((item["finding"][0], item["finding"][1]))
            if item["finding"][0] == "WORKSHEET_NAME":
                worksheet = next(
                    f.worksheet
                    for f in fixture.findings
                    if (f.ruleId, f.target) == tuple(item["finding"])
                )
                renames[worksheet] = item["value"]["name"]
    return expected_tuples(ctx, fixture, result["output_sha"], renames=renames, removed=removed)


def check_output(ctx: SuiteContext, result: Json, action: str) -> Json:
    """Output version metadata, package comparison and the output audit's exact findings."""
    status, ids = result["status"], result["ids"]
    output_version = version(
        ctx, ids, f"{action} output version", version_id=status["outputVersionId"]
    )
    if output_version["sourceVersionId"] != ids["versionId"]:
        raise CheckFailed(f"{action}: output version does not point to its exact source version.")
    if output_version["sha256"] != result["output_sha"]:
        raise CheckFailed(f"{action}: output version hash differs from the downloaded bytes.")
    details = plan_of(ctx, result["fixture"], result["plan"])
    try:
        problems = compare_packages(
            ctx.suite.bytes_of(result["fixture"]), result["output"], details["effects"]
        )
    except PackageError as exc:
        problems = [f"output package unreadable ({exc})"]
    # The output audit is a separate resource: record it before judging preservation, so a
    # preservation defect does not hide the audit from independent cases (section 15.3).
    audit = output_audit_of(ctx, result, action)
    if problems:
        raise CheckFailed(f"{action}: {problems[0]} ({len(problems)} problem(s)).")
    check_output_findings(ctx, result, audit, action)
    return audit


def output_audit_of(ctx: SuiteContext, result: Json, action: str) -> Json:
    if "output_audit" not in result:
        status = result["status"]
        result["output_audit"] = poll_audit(
            ctx,
            result["ids"],
            f"{action} output audit",
            audit_id=status["auditId"],
            version_id=status["outputVersionId"],
        )
    audit: Json = result["output_audit"]
    return audit


def check_output_findings(ctx: SuiteContext, result: Json, audit: Json, action: str) -> None:
    expected = expected_output(ctx, result)
    if audit["status"] != status_for(expected):
        raise CheckFailed(
            f"{action}: output audit ended {audit['status']}; expected {status_for(expected)}."
        )
    actual = actual_tuples(audit)
    if actual != expected:
        raise CheckFailed(f"{action} output audit: {describe(actual, expected)}")


def report_records(ctx: SuiteContext, result: Json) -> tuple[list[Json], list[Json]]:
    """Canonical report decision records and committed APPLY changes, in source finding order."""
    fixture = ctx.suite.accepted(result["fixture"])
    order = {
        (f.ruleId, f.target): index
        for index, f in enumerate(
            sorted(fixture.findings, key=lambda f: (f.ruleId, f.worksheet, f.target))
        )
    }
    table = extras(ctx, result["fixture"], "decisions")
    names = plan_of(ctx, result["fixture"], result["plan"])["decisions"]
    lookup = finding_lookup(ctx, result["fixture"])
    records = []
    for name in sorted(names, key=lambda n: order[tuple(table[n]["finding"])]):
        item = table[name]
        source = next(f for f in fixture.findings if (f.ruleId, f.target) == tuple(item["finding"]))
        records.append(
            {
                "sourceAuditId": result["audit"]["auditId"],
                "findingId": lookup[(source.ruleId, source.target)],
                "ruleId": source.ruleId,
                "worksheet": source.worksheet,
                "target": source.target,
                "action": item["action"],
                "value": item.get("value"),
            }
        )
    return records, [record for record in records if record["action"] == "APPLY"]


# --- fault session (section 14.1) ----------------------------------------------------------------


def setup_session(
    ctx: SuiteContext,
    setup: Json,
    *,
    identity: Identity | None = "test-admin",
    raw_token: str | None = None,
    session_id: str | None = None,
) -> tuple[str, httpx.Response]:
    session_id = session_id or f"t9-{uuid.uuid4().hex}"
    # Recorded before sending: a lost response may still have created the namespace, and
    # cleaning an unknown ID is an idempotent no-op (section 14).
    pending: list[str] = ctx.state.setdefault("aux_sessions", [])
    pending.append(session_id)
    response = ctx.client.request(
        "POST",
        "/test/setup",
        identity=identity,
        raw_token=raw_token,
        json={"session_id": session_id, "setup": setup},
        timeout=limit(ctx, "system_setup_seconds", 30),
    )
    if response.status_code != 201:
        pending.remove(session_id)  # an answered refusal created nothing to clean
    return session_id, response


def unknown_session(ctx: SuiteContext, session_id: str, action: str) -> None:
    """A refused setup created no runnable namespace: a project call naming it returns 400."""
    response = call(ctx, "GET", "/api/v1/workbooks/t9-probe/versions/t9-probe", session=session_id)
    project_error(ctx, response, 400, f"{action} session probe")


def fault_setup(ctx: SuiteContext) -> str:
    session_id, response = setup_session(ctx, {"faults": {"audit_failures": 1}})
    ctx.qualify()
    if response.status_code == 403:
        raise CheckFailed(
            "The target refused the required isolated audit-failure capability "
            "(testing 403); this is a failed capability check, not a skip."
        )
    expect_status(response, 201, "Fault session setup")
    echoed = body_of(response, "SetupResponse", "Fault session setup")
    if echoed["session_id"] != session_id:
        raise CheckFailed("Fault session setup did not echo the supplied session_id.")
    ctx.state["fault_session"] = session_id
    return f"Fault session {session_id} was created with audit_failures 1 and echoed exactly."


def fault_envelope(ctx: SuiteContext) -> str:
    session = ctx.state.get("fault_session")
    if session is None:
        raise Prerequisite("Not executed: the fault session setup did not succeed.")
    fixture = str(ctx.parameters["fixture"])
    ids = upload(ctx, fixture, "Fault-session upload", session=session)
    audit = poll_audit(ctx, ids, "Fault-session audit", session=session, allow_failure=True)
    if audit["status"] != "PROCESSING_FAILED":
        raise CheckFailed(
            f"The armed fault session audit ended {audit['status']}, not PROCESSING_FAILED."
        )
    # The failed audit exists now; recovery can proceed even if a later assertion here fails.
    ctx.state["fault"] = {"ids": ids, "audit": audit, "report": None}
    error = audit["error"]
    if (error["code"], error["message"], error["details"]) != (
        "TEST_AUDIT_FAILURE",
        FAULT_MESSAGE,
        FAULT_DETAILS,
    ):
        raise CheckFailed("The failed audit does not carry the exact TEST_AUDIT_FAILURE error.")
    if audit["findings"] or audit["completedAt"] is None:
        raise CheckFailed("The failed audit exposes findings or lacks completedAt.")
    report_raw, report = report_bytes(
        ctx, ids, ids["auditId"], "Failed audit report", session=session
    )
    if (report["status"], report["checksPerformed"], report["error"]) != (
        "PROCESSING_FAILED",
        [],
        error,
    ):
        raise CheckFailed(
            "The failed report does not show PROCESSING_FAILED, no checks and the same error."
        )
    source, _ = download(
        ctx, f"{version_path(ids)}/source", "Fault-session source", session=session
    )
    if source != ctx.suite.bytes_of(fixture):
        raise CheckFailed("The fault-session source download differs from the uploaded bytes.")
    current = version(ctx, ids, "Fault-session version", session=session)
    if current["latestAuditId"] != ids["auditId"] or current["status"] != "PROCESSING_FAILED":
        raise CheckFailed("The version does not record the failed latest audit.")
    ctx.state["fault"]["report"] = report_raw
    return (
        f"Fault session {session}: injected PROCESSING_FAILED envelope, failed report and "
        "unchanged source verified."
    )


def fault_recovery(ctx: SuiteContext) -> str:
    fault = ctx.state.get("fault")
    if fault is None:
        raise Prerequisite("Not executed: no injected failed audit was observed.")
    session = ctx.state["fault_session"]
    ids = fault["ids"]
    response = call(ctx, "POST", f"{version_path(ids)}/audits", session=session)
    accepted = ok(ctx, response, 202, "ReauditResponse", "Fault-session manual re-audit")
    fresh(ctx, [accepted["auditId"]], "Fault-session manual re-audit")
    audit = poll_audit(
        ctx, ids, "Fault-session re-audit", audit_id=accepted["auditId"], session=session
    )
    check_audit(ctx, audit, str(ctx.parameters["fixture"]), "Recovered audit")
    old = call(ctx, "GET", audit_path(ids), session=session)
    expect_status(old, 200, "Historical failed audit")
    if body_of(old, "AuditResponse", "Historical failed audit") != fault["audit"]:
        raise CheckFailed("The historical failed audit changed after recovery.")
    raw, _ = report_bytes(ctx, ids, ids["auditId"], "Historical failed report", session=session)
    if fault["report"] is not None and raw != fault["report"]:
        raise CheckFailed("The historical failed report bytes changed after recovery.")
    current = version(ctx, ids, "Recovered version", session=session)
    if current["latestAuditId"] != accepted["auditId"]:
        raise CheckFailed("latestAuditId does not identify the recovering audit.")
    record = history(ctx, ids, "Fault-session history", session=session)
    audits = [(a["auditId"], a["status"]) for a in record["audits"]]
    if audits != [(ids["auditId"], "PROCESSING_FAILED"), (accepted["auditId"], audit["status"])]:
        raise CheckFailed("History does not list the failed audit and then the recovering audit.")
    # Other sessions are unaffected: the main session's audits completed normally.
    main = ctx.state.get("p4:audit:seeded_all_six")
    if main is not None and main["status"] == "PROCESSING_FAILED":
        raise CheckFailed("The fault leaked into the main session.")
    return "The one-shot fault was consumed; the manual re-audit completed normally."


def fault_auth(ctx: SuiteContext) -> str:
    cases: list[tuple[str, Identity | None, str | None, int]] = [
        ("user", "userA", None, 403),
        ("reviewer", "reviewer", None, 403),
        ("unauthenticated", None, None, 401),
        ("invalid token", None, INVALID_TOKEN, 401),
    ]
    for label, identity, token, status in cases:
        session_id, response = setup_session(
            ctx, {"faults": {"audit_failures": 1}}, identity=identity, raw_token=token
        )
        testing_error(ctx, response, status, f"Fault setup by {label}", None)
        unknown_session(ctx, session_id, f"Fault setup by {label}")
    return (
        "User/reviewer fault setup returned 403 and missing/invalid credentials 401; no namespace."
    )


def fault_invalid(ctx: SuiteContext) -> str:
    variants: list[Json] = [
        {},
        {"audit_failures": 2},
        {"audit_failures": 0},
        {"audit_failures": True},
        {"audit_failures": "1"},
        {"audit_failures": 1, "extra": 1},
        {"unknown_fault": 1},
    ]
    for faults in variants:
        session_id, response = setup_session(ctx, {"faults": faults})
        testing_error(ctx, response, 400, f"Fault setup {json.dumps(faults)}", "system")
        unknown_session(ctx, session_id, f"Fault setup {json.dumps(faults)}")
    return f"All {len(variants)} malformed fault objects returned 400 source system; no namespace."


# --- uploads and audits ----------------------------------------------------------------------


def fixture_upload(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ctx.state[f"p4:upload:{fixture}"] = upload(ctx, fixture, f"{fixture} upload")
    return f"{fixture} was accepted with 202 and fresh PROCESSING identifiers."


def fixture_audit(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ids = ctx.state.get(f"p4:upload:{fixture}")
    if ids is None:
        raise Prerequisite(f"Not executed: the {fixture} upload produced no identifiers.")
    ctx.qualify()
    audit = poll_audit(ctx, ids, f"{fixture} audit")
    ctx.state[f"p4:audit:{fixture}"] = audit
    expected = check_audit(ctx, audit, fixture, f"{fixture} audit")
    return f"{fixture} returned exactly the {len(expected)} reviewed findings in contract order."


def fixture_upload_audit(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ids = upload(ctx, fixture, f"{fixture} upload")
    audit = poll_audit(ctx, ids, f"{fixture} audit")
    expected = check_audit(ctx, audit, fixture, f"{fixture} audit")
    if audit["status"] == "REVIEW_REQUIRED":
        response = call(ctx, "GET", f"{audit_path(ids)}/review")
        review = ok(ctx, response, 200, "ReviewResponse", f"{fixture} review context")
        if any("APPLY" in item["allowedActions"] for item in review["items"]):
            raise CheckFailed(f"{fixture} review context advertises APPLY for a manual finding.")
    return f"{fixture} completed {audit['status']} with {len(expected)} expected finding(s)."


def rule_vector(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    case = str(ctx.parameters["case"])
    cases = extras(ctx, fixture, "cases")
    if case not in cases:
        raise Prerequisite(f"Not executed: {fixture} declares no {case} case.")
    ids, audit = audited(ctx, fixture, f"vector:{fixture}")
    ctx.qualify()
    accepted = ctx.suite.accepted(fixture)
    sheets = set(cases[case]["worksheets"])
    expected = [row for row in expected_tuples(ctx, accepted, accepted.sha256) if row[3] in sheets]
    actual = [row for row in actual_tuples(audit) if row[3] in sheets]
    if actual != expected:
        raise CheckFailed(f"{case}: {describe(actual, expected)}")
    limitations = audit["limitations"]
    if limitations != sorted(set(limitations)):
        raise CheckFailed(f"{case}: limitations are not deduplicated and code-point sorted.")
    if cases[case].get("requires_limitation") and not limitations:
        raise CheckFailed(f"{case}: the required limitation/disclosure is missing.")
    return f"{case}: {len(expected)} finding(s) on {sorted(sheets)} match the reviewed vector."


# --- review context ----------------------------------------------------------------------------


def review_context(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ids, audit = audited(ctx, fixture, f"review:{fixture}")
    response = call(ctx, "GET", f"{audit_path(ids)}/review")
    review = ok(ctx, response, 200, "ReviewResponse", "Owner review context")
    if (review["workbookId"], review["versionId"], review["auditId"]) != (
        ids["workbookId"],
        ids["versionId"],
        ids["auditId"],
    ):
        raise CheckFailed("Review context names a different workbook/version/audit.")
    if [item["findingId"] for item in review["items"]] != [
        f["findingId"] for f in audit["findings"]
    ]:
        raise CheckFailed("Review items are not ordered exactly like the audit findings.")
    lookup = finding_lookup(ctx, fixture)
    expected = extras(ctx, fixture, "review")
    by_id = {item["findingId"]: item for item in review["items"]}
    for entry in expected:
        item = by_id.get(lookup[tuple(entry["finding"])])
        if item is None:
            raise CheckFailed(f"Review context lacks {entry['finding'][0]} {entry['finding'][1]}.")
        if (
            item["allowedActions"] != entry["allowedActions"]
            or item["currentValue"] != entry["currentValue"]
        ):
            raise CheckFailed(
                f"Review item {entry['finding'][0]} {entry['finding'][1]} has different allowed "
                "actions or current value."
            )
    ctx.state[f"p4:review-body:{fixture}"] = review
    return f"Owner review context matched {len(expected)} reviewed items in audit order."


def review_reviewer(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ids, audit = audited(ctx, fixture, f"review:{fixture}")
    owner = ctx.state.get(f"p4:review-body:{fixture}")
    response = call(ctx, "GET", f"{audit_path(ids)}/review", identity="reviewer")
    review = ok(ctx, response, 200, "ReviewResponse", "Granted reviewer review context")
    if any(item["allowedActions"] for item in review["items"]):
        raise CheckFailed("A read-only reviewer was offered decision actions.")
    if owner is not None and [(i["findingId"], i["currentValue"]) for i in review["items"]] != [
        (i["findingId"], i["currentValue"]) for i in owner["items"]
    ]:
        raise CheckFailed("Reviewer review context shows different current values.")
    before = history(ctx, ids, "History before reviewer decisions")
    names = list(extras(ctx, fixture, "decisions"))[:1]
    body = decision_body(ctx, fixture, audit["auditId"], names)
    project_error(
        ctx,
        submit(ctx, ids, body, key=new_key(), identity="reviewer"),
        403,
        "Reviewer decision batch",
        "FORBIDDEN",
    )
    if history(ctx, ids, "History after reviewer decisions") != before:
        raise CheckFailed("A refused reviewer decision batch changed the workbook history.")
    return "Granted reviewer saw current values with no actions and could not submit decisions."


# --- versions and re-audits ----------------------------------------------------------------------


def _new_version(
    ctx: SuiteContext,
    ids: Mapping[str, str],
    data: bytes,
    name: str,
    action: str,
    *,
    identity: Identity | None = "userA",
    raw_token: str | None = None,
    session: SessionChoice = "main",
) -> httpx.Response:
    return call(
        ctx,
        "POST",
        f"/api/v1/workbooks/{ids['workbookId']}/versions",
        identity=identity,
        raw_token=raw_token,
        session=session,
        files=multipart(data, name),
    )


def version_new(ctx: SuiteContext) -> str:
    fixture, changed = str(ctx.parameters["fixture"]), str(ctx.parameters["changed"])
    with setup(ctx):
        ids = upload(ctx, fixture, f"{fixture} upload")
        first_audit = poll_audit(ctx, ids, f"{fixture} audit")
        before = version(ctx, ids, "Original version")
    name = ctx.suite.accepted(fixture).upload_name
    response = _new_version(ctx, ids, ctx.suite.bytes_of(changed), name, "New version upload")
    added = ok(ctx, response, 202, "UploadResponse", "New version upload")
    if added["workbookId"] != ids["workbookId"]:
        raise CheckFailed("Operation 3 did not keep the existing workbook ID.")
    fresh(ctx, [added["versionId"], added["auditId"]], "New version upload")
    audit = poll_audit(ctx, added, "New version audit")
    check_audit(ctx, audit, changed, "New version audit")
    current = version(ctx, added, "New version")
    if (current["fileName"], current["sha256"], current["sourceVersionId"]) != (
        name,
        source_sha(ctx, changed),
        None,
    ):
        raise CheckFailed("New version metadata differs (filename, hash or null parent).")
    if version(ctx, ids, "Original version after operation 3") != before:
        raise CheckFailed("The earlier version changed after a new version was uploaded.")
    old = call(ctx, "GET", audit_path(ids))
    expect_status(old, 200, "Original audit")
    if body_of(old, "AuditResponse", "Original audit") != first_audit:
        raise CheckFailed("The earlier audit changed after a new version was uploaded.")
    record = history(ctx, ids, "History after operation 3")
    if [v["versionId"] for v in record["versions"]] != [ids["versionId"], added["versionId"]]:
        raise CheckFailed("History versions are not in creation order.")
    if [a["auditId"] for a in record["audits"]] != [ids["auditId"], added["auditId"]]:
        raise CheckFailed("History audits are not in acceptance order.")
    return "Operation 3 kept the workbook ID, issued fresh IDs and left earlier records unchanged."


def version_same_bytes(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    with setup(ctx):
        ids = upload(ctx, fixture, f"{fixture} upload")
        poll_audit(ctx, ids, f"{fixture} audit")
    data, name = ctx.suite.bytes_of(fixture), ctx.suite.accepted(fixture).upload_name
    added = ok(
        ctx,
        _new_version(ctx, ids, data, name, "Same-bytes version"),
        202,
        "UploadResponse",
        "Same-bytes version",
    )
    fresh(ctx, [added["versionId"], added["auditId"]], "Same-bytes version")
    poll_audit(ctx, added, "Same-bytes version audit")
    current = version(ctx, added, "Same-bytes version metadata")
    if current["sha256"] != source_sha(ctx, fixture) or current["sourceVersionId"] is not None:
        raise CheckFailed("Same-bytes version has a different hash or a non-null parent.")
    return "Identical bytes uploaded as a new version received fresh version/audit IDs."


def _reaudit(
    ctx: SuiteContext,
    ids: Mapping[str, str],
    action: str,
    body: Json | None,
    *,
    version_id: str | None = None,
    absent: bool = False,
) -> Json:
    kwargs: dict[str, Any] = {} if absent else {"json": body}
    response = call(ctx, "POST", f"{version_path(ids, version_id)}/audits", **kwargs)
    accepted = ok(ctx, response, 202, "ReauditResponse", action)
    if accepted["versionId"] != (version_id or ids["versionId"]):
        raise CheckFailed(f"{action} created the audit on a different version.")
    if accepted["rulesetVersion"] != ctx.suite.manifest.ruleset_version:
        raise CheckFailed(f"{action} used ruleset {accepted['rulesetVersion']}.")
    fresh(ctx, [accepted["auditId"]], action)
    return accepted


def reaudit(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    with setup(ctx):
        ids = upload(ctx, fixture, f"{fixture} upload")
        first = poll_audit(ctx, ids, f"{fixture} audit")
        first_report, _ = report_bytes(ctx, ids, ids["auditId"], "First report")
    trials: list[tuple[str, Json | None, bool]] = [
        ("absent body", None, True),
        ("empty object", {}, False),
        ("MANUAL_REAUDIT reason", {"reason": "MANUAL_REAUDIT"}, False),
    ]
    for label, body, absent in trials:
        accepted = _reaudit(ctx, ids, f"Re-audit with {label}", body, absent=absent)
        audit = poll_audit(ctx, ids, f"Re-audit with {label}", audit_id=accepted["auditId"])
        if actual_tuples(audit) != actual_tuples(first):
            raise CheckFailed(
                f"Re-audit with {label} returned different findings for the same bytes."
            )
        if version(ctx, ids, "Version after re-audit")["latestAuditId"] != accepted["auditId"]:
            raise CheckFailed(f"latestAuditId was not updated by the re-audit with {label}.")
    old = call(ctx, "GET", audit_path(ids))
    expect_status(old, 200, "Original audit after re-audits")
    if body_of(old, "AuditResponse", "Original audit after re-audits") != first:
        raise CheckFailed("The original audit changed after manual re-audits.")
    if report_bytes(ctx, ids, ids["auditId"], "First report again")[0] != first_report:
        raise CheckFailed("The original report bytes changed after manual re-audits.")
    return "Three accepted re-audit forms created fresh audits on the same version; history intact."


def reaudit_invalid(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    with setup(ctx):
        ids = upload(ctx, fixture, f"{fixture} upload")
        poll_audit(ctx, ids, f"{fixture} audit")
    before = history(ctx, ids, "History before invalid re-audits")
    for label, body in (
        ("wrong reason", {"reason": "SCHEDULED"}),
        ("unknown field", {"reason": "MANUAL_REAUDIT", "force": True}),
    ):
        response = call(ctx, "POST", f"{version_path(ids)}/audits", json=body)
        project_error(ctx, response, 400, f"Re-audit with {label}", "INVALID_REQUEST")
    if history(ctx, ids, "History after invalid re-audits") != before:
        raise CheckFailed("A rejected re-audit request created an audit.")
    return "Wrong reason and unknown field returned 400 INVALID_REQUEST with no extra audit."


def reaudit_determinism(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    with setup(ctx):
        ids = upload(ctx, fixture, f"{fixture} upload")
        first = poll_audit(ctx, ids, f"{fixture} audit")
    for run in range(1, 6):
        accepted = _reaudit(ctx, ids, f"Determinism re-audit {run}", None, absent=True)
        audit = poll_audit(ctx, ids, f"Determinism re-audit {run}", audit_id=accepted["auditId"])
        ctx.qualify()
        if (audit["findings"], audit["limitations"], audit["status"]) != (
            first["findings"],
            first["limitations"],
            first["status"],
        ):
            raise CheckFailed(
                f"Manual re-audit {run} differed in findings, prose, limitations or status."
            )
    return "Five manual re-audits matched the first audit exactly, with fresh audit IDs."


# --- downloads and preservation ------------------------------------------------------------------


def source_before(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ids, audit = audited(ctx, fixture, f"preserve:{fixture}")
    with setup(ctx):
        report, _ = report_bytes(ctx, ids, ids["auditId"], "Source report before remediation")
    ctx.state[f"p4:before:{fixture}"] = {"audit": audit, "report": report}
    source, _ = download(ctx, f"{version_path(ids)}/source", "Source download")
    if source != ctx.suite.bytes_of(fixture):
        raise CheckFailed("The source download differs from the uploaded bytes.")
    return "Source download returned the exact uploaded bytes as an XLSX attachment."


def _preserve(ctx: SuiteContext) -> Json:
    fixture = str(ctx.parameters["fixture"])
    plan = next(iter(extras(ctx, fixture, "plans")))
    return run_plan(ctx, fixture, plan, f"preserve:{fixture}")


def output_hash(ctx: SuiteContext) -> str:
    result = _preserve(ctx)
    status, ids = result["status"], result["ids"]
    again, headers = download(ctx, f"/api/v1/outputs/{status['outputId']}/workbook", "Output again")
    if again != result["output"]:
        raise CheckFailed("Repeated output downloads are not byte-identical.")
    via_source, _ = download(
        ctx, f"{version_path(ids, status['outputVersionId'])}/source", "Output version source path"
    )
    if via_source != result["output"]:
        raise CheckFailed("The output version's source path returns different bytes.")
    output_version = version(ctx, ids, "Output version", version_id=status["outputVersionId"])
    if output_version["sha256"] != result["output_sha"]:
        raise CheckFailed("Output version metadata hash differs from the downloaded bytes.")
    base = ctx.suite.accepted(result["fixture"]).upload_name.removesuffix(".xlsx")
    if f"{base}-remediated.xlsx" not in headers.get("content-disposition", ""):
        raise CheckFailed("Output filename is not the original basename plus -remediated.xlsx.")
    return "Output bytes are stable, match the output version hash and both download paths."


def source_after(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    before = ctx.state.get(f"p4:before:{fixture}")
    if before is None:
        raise Prerequisite("Not executed: no pre-remediation source evidence was recorded.")
    result = _preserve(ctx)
    ids = result["ids"]
    source, _ = download(ctx, f"{version_path(ids)}/source", "Source after remediation")
    if source != ctx.suite.bytes_of(fixture) or version(ctx, ids, "Source version")[
        "sha256"
    ] != source_sha(ctx, fixture):
        raise CheckFailed("The source bytes or hash changed after remediation.")
    old = call(ctx, "GET", audit_path(ids))
    expect_status(old, 200, "Source audit after remediation")
    if body_of(old, "AuditResponse", "Source audit after remediation") != before["audit"]:
        raise CheckFailed("The historical source audit changed after remediation.")
    if (
        report_bytes(ctx, ids, ids["auditId"], "Source report after remediation")[0]
        != before["report"]
    ):
        raise CheckFailed("The historical source report bytes changed after remediation.")
    return "Source bytes, hash, audit and report were identical before and after remediation."


def preservation(ctx: SuiteContext) -> str:
    ctx.qualify()
    result = _preserve(ctx)
    check_output(ctx, result, "Preservation output")
    return "The independent comparator found exactly the approved differences and nothing else."


def download_large(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ids, _ = audited(ctx, fixture, f"large:{fixture}")
    source, _ = download(ctx, f"{version_path(ids)}/source", "Large source download")
    if source != ctx.suite.bytes_of(fixture) or len(source) <= 2_097_152:
        raise CheckFailed("The large source download was not the complete >2 MiB upload.")
    plan = next(iter(extras(ctx, fixture, "plans")))
    result = run_plan(ctx, fixture, plan, f"large:{fixture}")
    if len(result["output"]) <= 2_097_152:
        raise CheckFailed(
            "The large output is not over 2 MiB; the transport limit was not exercised."
        )
    check_output(ctx, result, "Large output")
    sizes = f"{len(source):,}-byte source and {len(result['output']):,}-byte output"
    return f"A {sizes} downloaded intact."


# --- decisions, remediation, reports and history -------------------------------------------------


def decisions_mixed(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ctx.qualify()
    result = run_plan(ctx, fixture, "mixed", f"mixed:{fixture}")
    # Published for dependents as soon as the remediation exists; this case's own accuracy
    # checks below do not gate independent cases (section 15.3).
    ctx.state["mixed_remediation"] = result
    if result["accepted"]["status"] != "REMEDIATING":
        raise CheckFailed("RemediationAccepted did not report REMEDIATING.")
    check_output(ctx, result, "Mixed decisions")
    source = version(ctx, result["ids"], "Source version after remediation")
    if source["status"] != ctx.suite.accepted(fixture).expected_status:
        raise CheckFailed("The source version's stable status was rewritten by the remediation.")
    return "Approved, declined, manual and omitted decisions produced only the approved edit."


def apply_correction(ctx: SuiteContext) -> str:
    fixture, plan = str(ctx.parameters["fixture"]), str(ctx.parameters["plan"])
    ctx.qualify()
    result = run_plan(ctx, fixture, plan, f"apply:{fixture}:{plan}")
    check_output(ctx, result, f"{plan} correction")
    return f"The {plan} correction took effect with no unexplained package difference."


def apply_no_edits(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ctx.qualify()
    result = run_plan(ctx, fixture, "no_edits", f"noedit:{fixture}")
    if result["output"] != ctx.suite.bytes_of(fixture):
        raise CheckFailed("An all DECLINE/MANUAL_REVIEW batch changed the workbook bytes.")
    if result["status"]["outputVersionId"] == result["ids"]["versionId"]:
        raise CheckFailed("The no-edit output reused the source version identity.")
    audit = check_output(ctx, result, "No-edit output")
    _, report = report_bytes(
        ctx,
        result["ids"],
        result["status"]["auditId"],
        "No-edit report",
        version_id=result["status"]["outputVersionId"],
    )
    if report["approvedChanges"] or report["unresolvedFindings"] != audit["findings"]:
        raise CheckFailed("The no-edit report shows approved changes or omits unresolved findings.")
    return "A decline/manual-only batch produced a separate byte-identical output and report."


def output_audit(ctx: SuiteContext) -> str:
    result = ctx.state.get("mixed_remediation")
    if result is None:
        raise Prerequisite("Not executed: the mixed remediation did not complete.")
    ctx.qualify()
    audit = output_audit_of(ctx, result, "Output audit")
    if audit["rulesetVersion"] != ctx.suite.manifest.ruleset_version:
        raise CheckFailed("The output audit used a different ruleset.")
    check_output_findings(ctx, result, audit, "Output audit")
    return "The output audit ran all rules on the output bytes; only the approved finding is gone."


def report_complete(ctx: SuiteContext) -> str:
    result = ctx.state.get("mixed_remediation")
    if result is None or "output_audit" not in result:
        raise Prerequisite("Not executed: the mixed remediation output audit is unavailable.")
    status, ids, audit = result["status"], result["ids"], result["output_audit"]
    _, report = report_bytes(
        ctx, ids, status["auditId"], "Output report", version_id=status["outputVersionId"]
    )
    decisions, approved = report_records(ctx, result)
    output_version = version(ctx, ids, "Output version", version_id=status["outputVersionId"])
    expected = {
        "reportVersion": contract.REPORT_VERSION,
        "specificationVersion": contract.SPECIFICATION_VERSION,
        "workbookId": ids["workbookId"],
        "versionId": status["outputVersionId"],
        "auditId": status["auditId"],
        "fileName": output_version["fileName"],
        "sha256": result["output_sha"],
        "sourceVersionId": ids["versionId"],
        "rulesetVersion": ctx.suite.manifest.ruleset_version,
        "status": audit["status"],
        "checksPerformed": list(RULE_ORDER),
        "findings": audit["findings"],
        "decisions": decisions,
        "approvedChanges": approved,
        "unresolvedFindings": audit["findings"],
        "manualReviewItems": [
            f for f in audit["findings"] if f["classification"] == "MANUAL_REVIEW"
        ],
        "limitations": audit["limitations"],
        "remediationId": status["remediationId"],
        "outputVersionId": status["outputVersionId"],
        "outputId": status["outputId"],
        "error": None,
    }
    for field, value in expected.items():
        if report[field] != value:
            raise CheckFailed(f"Output report field {field} does not match the actual records.")
    if contract.parse_timestamp(report["auditDate"]) != contract.parse_timestamp(
        audit["completedAt"]
    ):
        raise CheckFailed("Output report auditDate is not the audit completion time.")
    _, source_report = report_bytes(ctx, ids, result["audit"]["auditId"], "Source report")
    if (
        source_report["decisions"],
        source_report["approvedChanges"],
        source_report["remediationId"],
    ) != ([], [], None):
        raise CheckFailed("The source upload report claims remediation provenance.")
    return "The output report matches its findings, canonical decisions, changes and provenance."


def report_stable(ctx: SuiteContext) -> str:
    result = ctx.state.get("mixed_remediation")
    if result is None:
        raise Prerequisite("Not executed: the mixed remediation did not complete.")
    status, ids = result["status"], result["ids"]
    first, _ = report_bytes(
        ctx, ids, status["auditId"], "Output report", version_id=status["outputVersionId"]
    )
    if (
        report_bytes(
            ctx, ids, status["auditId"], "Output report again", version_id=status["outputVersionId"]
        )[0]
        != first
    ):
        raise CheckFailed("Repeated output report downloads are not byte-identical.")
    if (
        report_bytes(ctx, ids, result["audit"]["auditId"], "Source report")[0]
        != result["source_report"]
    ):
        raise CheckFailed("The source report changed after decisions were made.")
    accepted = _reaudit(
        ctx, ids, "Output version re-audit", None, version_id=status["outputVersionId"], absent=True
    )
    # Recorded as soon as it is accepted: history and status checks must account for it even
    # if its polling later fails.
    result["output_reaudit_id"] = accepted["auditId"]
    poll_audit(
        ctx,
        ids,
        "Output version re-audit",
        audit_id=accepted["auditId"],
        version_id=status["outputVersionId"],
    )
    if (
        report_bytes(
            ctx,
            ids,
            status["auditId"],
            "Output report after re-audit",
            version_id=status["outputVersionId"],
        )[0]
        != first
    ):
        raise CheckFailed("A later re-audit rewrote the earlier output report.")
    _, newer = report_bytes(
        ctx, ids, accepted["auditId"], "Re-audit report", version_id=status["outputVersionId"]
    )
    if (newer["remediationId"], newer["outputId"]) != (status["remediationId"], status["outputId"]):
        raise CheckFailed("A re-audit of the generated version lost its remediation provenance.")
    return (
        "Reports are byte-stable; a later re-audit kept provenance and left earlier reports alone."
    )


def history_check(ctx: SuiteContext) -> str:
    result = ctx.state.get("mixed_remediation")
    if result is None:
        raise Prerequisite("Not executed: the mixed remediation did not complete.")
    ids, status = result["ids"], result["status"]
    record = history(ctx, ids, "Workbook history")
    ctx.qualify()
    versions = [(v["versionId"], v["sourceVersionId"]) for v in record["versions"]]
    if versions != [(ids["versionId"], None), (status["outputVersionId"], ids["versionId"])]:
        raise CheckFailed("History versions are not the upload then its generated output.")
    audits = [a["auditId"] for a in record["audits"]]
    expected = [ids["auditId"], status["auditId"]]
    if "output_reaudit_id" in result:
        expected.append(result["output_reaudit_id"])
    if audits != expected:
        raise CheckFailed("History audits are not every audit in acceptance order.")
    for entry in record["versions"]:
        current = version(ctx, ids, "History version check", version_id=entry["versionId"])
        if current["status"] != entry["status"]:
            raise CheckFailed("A history version status differs from operation 4.")
    return "History lists the upload, its generated output and all audits in sequence order."


def remediated_status(ctx: SuiteContext) -> str:
    result = ctx.state.get("mixed_remediation")
    if result is None:
        raise Prerequisite("Not executed: the mixed remediation did not complete.")
    ids, status = result["ids"], result["status"]
    output_version = version(ctx, ids, "Generated version", version_id=status["outputVersionId"])
    ctx.qualify()
    if output_version["status"] != "REMEDIATED":
        raise CheckFailed(
            f"Generated version status is {output_version['status']}, not REMEDIATED."
        )
    if output_version["latestAuditId"] is None:
        raise CheckFailed("The generated version reports no latest audit.")
    audit = poll_audit(
        ctx,
        ids,
        "Generated version latest audit",
        audit_id=output_version["latestAuditId"],
        version_id=status["outputVersionId"],
    )
    if audit["status"] == "AUDIT_COMPLETE" or not audit["findings"]:
        raise CheckFailed("The generated version's audit unexpectedly reports no remaining issues.")
    return "REMEDIATED coexists with an output audit that still requires review."


# --- rejected decision batches ---------------------------------------------------------------


def _first(
    ctx: SuiteContext, fixture: str, rule: str, classification: str = "REQUIRES_USER_INPUT"
) -> tuple[str, str]:
    for finding in ctx.suite.accepted(fixture).findings:
        if finding.ruleId == rule and finding.classification == classification:
            return finding.ruleId, finding.target
    raise Prerequisite(f"Not executed: {fixture} has no {classification} {rule} finding.")


def _ids_for(ctx: SuiteContext, fixture: str) -> dict[tuple[str, str], str]:
    return finding_lookup(ctx, fixture)


VALID_ALT = {"title": "Chart", "description": "A column chart of two values."}


def _requests(
    ctx: SuiteContext, fixture: str, audit_id: str, condition: str
) -> list[tuple[str, Json, int, str]]:
    """(label, body, status, code) for each separately evaluated request in a condition."""
    ids = _ids_for(ctx, fixture)
    alt = ids[_first(ctx, fixture, "ALT_TEXT")]
    names = [
        ids[(f.ruleId, f.target)]
        for f in ctx.suite.accepted(fixture).findings
        if f.ruleId == "WORKSHEET_NAME" and f.classification == "REQUIRES_USER_INPUT"
    ]
    link = ids[_first(ctx, fixture, "LINK_TEXT")]
    header = ids[_first(ctx, fixture, "TABLE_HEADER")]
    merge = ids[_first(ctx, fixture, "MERGED_CELLS", "MANUAL_REVIEW")]

    def batch(*decisions: Json) -> Json:
        return {"auditId": audit_id, "decisions": list(decisions)}

    def apply(finding: str, value: Json) -> Json:
        return {"findingId": finding, "action": "APPLY", "value": value}

    def rename(value: str) -> tuple[str, Json, int, str]:
        return (
            f"name {value!r}",
            batch(apply(names[0], {"name": value})),
            422,
            "INVALID_CORRECTION",
        )

    if condition == "empty":
        return [("empty decisions", batch(), 400, "INVALID_REQUEST")]
    if condition == "unknown_field":
        return [
            (
                "envelope field",
                {**batch({"findingId": merge, "action": "DECLINE"}), "note": "x"},
                400,
                "INVALID_REQUEST",
            ),
            (
                "decision field",
                batch({"findingId": merge, "action": "DECLINE", "note": "x"}),
                400,
                "INVALID_REQUEST",
            ),
        ]
    if condition == "duplicate":
        decline = {"findingId": merge, "action": "DECLINE"}
        return [("same finding twice", batch(decline, dict(decline)), 400, "DUPLICATE_DECISION")]
    if condition == "unknown_finding":
        return [
            (
                "absent finding",
                batch({"findingId": "f" * 64, "action": "DECLINE"}),
                404,
                "FINDING_NOT_FOUND",
            )
        ]
    if condition == "value_missing":
        return [
            (
                "APPLY without value",
                batch({"findingId": alt, "action": "APPLY"}),
                400,
                "MISSING_VALUE",
            )
        ]
    if condition == "value_wrong_shape":
        return [
            ("string value", batch(apply(alt, "Chart")), 422, "INVALID_CORRECTION"),
            ("other rule's shape", batch(apply(alt, {"name": "Chart"})), 422, "INVALID_CORRECTION"),
            (
                "unknown value field",
                batch(apply(alt, {**VALID_ALT, "lang": "en"})),
                422,
                "INVALID_CORRECTION",
            ),
            ("null value", batch(apply(alt, None)), 422, "INVALID_CORRECTION"),
        ]
    if condition == "value_on_decline":
        return [
            (
                "DECLINE with value",
                batch({"findingId": merge, "action": "DECLINE", "value": {"text": "x"}}),
                400,
                "INVALID_REQUEST",
            ),
            (
                "MANUAL_REVIEW with value",
                batch({"findingId": merge, "action": "MANUAL_REVIEW", "value": {"text": "x"}}),
                400,
                "INVALID_REQUEST",
            ),
        ]
    if condition == "alt_blank":
        return [
            (
                "blank title",
                batch(apply(alt, {"title": "  ", "description": "Chart."})),
                422,
                "INVALID_CORRECTION",
            ),
            (
                "blank description",
                batch(apply(alt, {"title": "Chart", "description": "\t"})),
                422,
                "INVALID_CORRECTION",
            ),
        ]
    if condition == "alt_too_long":
        return [
            (
                "201-code-point title",
                batch(apply(alt, {"title": "é" * 201, "description": "Chart."})),
                422,
                "INVALID_CORRECTION",
            ),
            (
                "801-code-point description",
                batch(apply(alt, {"title": "Chart", "description": "📊" * 801})),
                422,
                "INVALID_CORRECTION",
            ),
        ]
    if condition == "name_invalid":
        if len(names) < 1:
            raise Prerequisite(f"Not executed: {fixture} has no supported rename.")
        return [
            rename("N" * 32),
            rename(" Units"),
            rename("Units/2026"),
            rename("Sheet7"),
            rename("history"),
            rename("sheet1"),
            rename("Sheet1"),
            rename("notes"),
        ]
    if condition == "name_swap":
        if len(names) < 2:
            raise Prerequisite(f"Not executed: {fixture} has fewer than two supported renames.")
        return [
            (
                "two sheets to one name",
                batch(apply(names[0], {"name": "Units"}), apply(names[1], {"name": "Units"})),
                422,
                "CONFLICTING_CORRECTIONS",
            )
        ]
    if condition == "link_ambiguous":
        return [
            (
                "still ambiguous label",
                batch(apply(link, {"text": "Click here."})),
                422,
                "INVALID_CORRECTION",
            )
        ]
    if condition == "header_row":
        return [
            ("wrong row", batch(apply(header, {"headerRow": 2})), 422, "INVALID_CORRECTION"),
            ("boolean row", batch(apply(header, {"headerRow": True})), 422, "INVALID_CORRECTION"),
            (
                "replacement labels",
                batch(apply(header, {"headerRow": 1, "labels": ["A", "B"]})),
                422,
                "INVALID_CORRECTION",
            ),
        ]
    if condition == "decisions_over":
        many = [
            {"findingId": hashlib.sha256(str(i).encode()).hexdigest(), "action": "DECLINE"}
            for i in range(1001)
        ]
        return [("1001 decisions", batch(*many), 400, "INVALID_REQUEST")]
    raise ValueError(f"unknown reject condition {condition}")


def _reject_base(ctx: SuiteContext, fixture: str) -> tuple[dict[str, str], Json]:
    """The shared reject base and its latest completed audit (resolving any pending re-audit)."""
    ids, audit = audited(ctx, fixture, f"reject:{fixture}")
    cache = f"p4:reject-latest:{fixture}"
    shared_failure(ctx, cache)
    pending = ctx.state.get(f"{cache}:pending")
    if pending is not None:
        with remember_failure(ctx, cache), setup(ctx):
            ctx.state[cache] = poll_audit(ctx, ids, "Pending re-audit", audit_id=pending)
        del ctx.state[f"{cache}:pending"]
    return ids, ctx.state.get(cache) or audit


def _competing_reaudit(
    ctx: SuiteContext, fixture: str, ids: Mapping[str, str], action: str
) -> Json:
    """Start a re-audit on the shared reject base and record it before anything can fail."""
    accepted = _reaudit(ctx, ids, action, None, absent=True)
    ctx.state[f"p4:reject-latest:{fixture}:pending"] = accepted["auditId"]
    return accepted


def _expect_rejected(
    ctx: SuiteContext,
    ids: Mapping[str, str],
    requests: list[tuple[str, Json, int, str]],
    *,
    key: str | None = "fresh",
    session: SessionChoice = "main",
) -> None:
    before = history(ctx, ids, "History before rejected batches", session=session)
    for label, body, status, code in requests:
        response = submit(ctx, ids, body, key=new_key() if key == "fresh" else key, session=session)
        project_error(ctx, response, status, f"Decision batch with {label}", code)
    if history(ctx, ids, "History after rejected batches", session=session) != before:
        raise CheckFailed("A rejected decision batch created a remediation, output or audit.")


def reject_decisions(ctx: SuiteContext) -> str:
    fixture, condition = str(ctx.parameters["fixture"]), str(ctx.parameters["condition"])
    if condition == "manual_apply":
        ids, audit = _reject_base(ctx, fixture)
        lookup = _ids_for(ctx, fixture)
        requests = [
            (
                f"APPLY to {item['finding'][0]} {item['finding'][1]}",
                {
                    "auditId": audit["auditId"],
                    "decisions": [
                        {
                            "findingId": lookup[tuple(item["finding"])],
                            "action": "APPLY",
                            "value": item["value"],
                        }
                    ],
                },
                422,
                "UNSUPPORTED_CORRECTION",
            )
            for item in extras(ctx, fixture, "unsupported_apply")
        ]
        _expect_rejected(ctx, ids, requests)
        return (
            f"All {len(requests)} APPLY decisions outside the safe profile returned 422 "
            "UNSUPPORTED_CORRECTION."
        )
    if condition == "missing_key":
        ids, audit = _reject_base(ctx, fixture)
        body = {
            "auditId": audit["auditId"],
            "decisions": [
                {
                    "findingId": _ids_for(ctx, fixture)[
                        _first(ctx, fixture, "MERGED_CELLS", "MANUAL_REVIEW")
                    ],
                    "action": "DECLINE",
                }
            ],
        }
        _expect_rejected(
            ctx, ids, [("no Idempotency-Key", body, 400, "IDEMPOTENCY_KEY_REQUIRED")], key=None
        )
        _expect_rejected(
            ctx,
            ids,
            [("malformed Idempotency-Key", body, 400, "IDEMPOTENCY_KEY_REQUIRED")],
            key="bad key!",
        )
        return "Absent and malformed Idempotency-Key returned 400 IDEMPOTENCY_KEY_REQUIRED."
    if condition == "cross_audit_finding":
        ids, audit = _reject_base(ctx, fixture)
        other = ctx.state.get("p4:audit:seeded_all_six")
        foreign = other["findings"][0]["findingId"] if other and other["findings"] else None
        if foreign is None or foreign in {f["findingId"] for f in audit["findings"]}:
            raise Prerequisite(
                "Not executed: no completed audit of different bytes supplied a finding."
            )
        _expect_rejected(
            ctx,
            ids,
            [
                (
                    "another audit's finding",
                    {
                        "auditId": audit["auditId"],
                        "decisions": [{"findingId": foreign, "action": "DECLINE"}],
                    },
                    404,
                    "FINDING_NOT_FOUND",
                )
            ],
        )
        return "A finding from a different version's audit returned 404 FINDING_NOT_FOUND."
    if condition == "stale_audit":
        ids, audit = _reject_base(ctx, fixture)
        with setup(ctx):
            _competing_reaudit(ctx, fixture, ids, "Re-audit making the old audit stale")
            _reject_base(ctx, fixture)
        merge = _ids_for(ctx, fixture)[_first(ctx, fixture, "MERGED_CELLS", "MANUAL_REVIEW")]
        _expect_rejected(
            ctx,
            ids,
            [
                (
                    "the earlier audit",
                    {
                        "auditId": audit["auditId"],
                        "decisions": [{"findingId": merge, "action": "DECLINE"}],
                    },
                    409,
                    "STALE_AUDIT",
                )
            ],
        )
        return "Decisions against an older audit after a newer audit returned 409 STALE_AUDIT."
    if condition == "audit_processing":
        ids, audit = _reject_base(ctx, fixture)
        merge = _ids_for(ctx, fixture)[_first(ctx, fixture, "MERGED_CELLS", "MANUAL_REVIEW")]
        with setup(ctx):
            before = history(ctx, ids, "History before competing batch")
            accepted = _competing_reaudit(ctx, fixture, ids, "Competing re-audit")
        response = submit(
            ctx,
            ids,
            {"auditId": audit["auditId"], "decisions": [{"findingId": merge, "action": "DECLINE"}]},
            key=new_key(),
        )
        stale = (
            response.status_code == 409
            and body_of(response, "ProjectError", "Competing batch")["code"] == "STALE_AUDIT"
        )
        still_active = False
        if stale:
            probe = call(ctx, "GET", audit_path(ids, accepted["auditId"]))
            expect_status(probe, 200, "Competing re-audit status")
            still_active = body_of(probe, "AuditResponse", "Competing re-audit")["status"] == (
                "PROCESSING"
            )
        with setup(ctx):
            _reject_base(ctx, fixture)
        if stale and still_active:
            ctx.qualify()
            raise CheckFailed(
                "STALE_AUDIT was returned while the re-audit was still active; section 10 "
                "checks the active operation first (409 ACTIVE_OPERATION)."
            )
        if stale:
            raise Prerequisite(
                "Not executed: the competing re-audit finished before the batch "
                "arrived, so no active operation could be observed."
            )
        project_error(
            ctx, response, 409, "Decision batch during an active audit", "ACTIVE_OPERATION"
        )
        after = history(ctx, ids, "History after competing batch")
        if [v["versionId"] for v in after["versions"]] != [
            v["versionId"] for v in before["versions"]
        ]:
            raise CheckFailed("A rejected competing batch created a version.")
        return "A batch during the version's active re-audit returned 409 ACTIVE_OPERATION."
    if condition == "audit_failed":
        with setup(ctx):
            session_id, response = setup_session(ctx, {"faults": {"audit_failures": 1}})
            if response.status_code != 201:
                raise Prerequisite(
                    "Not executed: a fault session for the failed-audit case was refused."
                )
            ids = upload(ctx, fixture, "Upload for the failed-audit case", session=session_id)
            failed = poll_audit(
                ctx, ids, "Injected failed audit", session=session_id, allow_failure=True
            )
        if failed["status"] != "PROCESSING_FAILED":
            raise Prerequisite("Not executed: the injected failure was not observed.")
        merge = _ids_for(ctx, fixture)[_first(ctx, fixture, "MERGED_CELLS", "MANUAL_REVIEW")]
        _expect_rejected(
            ctx,
            ids,
            [
                (
                    "a failed latest audit",
                    {
                        "auditId": ids["auditId"],
                        "decisions": [{"findingId": merge, "action": "DECLINE"}],
                    },
                    409,
                    "AUDIT_NOT_READY",
                )
            ],
            session=session_id,
        )
        return "Decisions against a failed latest audit returned 409 AUDIT_NOT_READY."
    ids, audit = _reject_base(ctx, fixture)
    requests = _requests(ctx, fixture, audit["auditId"], condition)
    _expect_rejected(ctx, ids, requests)
    return f"{len(requests)} {condition} request(s) got the exact status/code; nothing changed."


def batch_atomic(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    ids, audit = _reject_base(ctx, fixture)
    lookup = _ids_for(ctx, fixture)
    alt = lookup[_first(ctx, fixture, "ALT_TEXT")]
    rename = lookup[_first(ctx, fixture, "WORKSHEET_NAME")]
    body = {
        "auditId": audit["auditId"],
        "decisions": [
            {"findingId": alt, "action": "APPLY", "value": VALID_ALT},
            {"findingId": rename, "action": "APPLY", "value": {"name": "Bad/Name"}},
        ],
    }
    _expect_rejected(
        ctx, ids, [("one valid and one invalid APPLY", body, 422, "INVALID_CORRECTION")]
    )
    return "One invalid decision rejected the whole batch; the valid edit was not applied."


def idempotency(ctx: SuiteContext) -> str:
    fixture, case = str(ctx.parameters["fixture"]), str(ctx.parameters["case"])
    ids, audit = audited(ctx, fixture, f"idempotency:{case}")
    plan = plan_of(ctx, fixture, "mixed")
    body = decision_body(ctx, fixture, audit["auditId"], plan["decisions"])
    key = new_key("idem")
    if case == "invalid_first":
        bad = {
            "auditId": audit["auditId"],
            "decisions": [body["decisions"][0], body["decisions"][0]],
        }
        project_error(
            ctx,
            submit(ctx, ids, bad, key=key),
            400,
            "Invalid first use of a key",
            "DUPLICATE_DECISION",
        )
    first = submit(ctx, ids, body, key=key)
    accepted = ok(ctx, first, 202, "RemediationAccepted", "Keyed decision batch")
    if case == "replay":
        again = ok(
            ctx, submit(ctx, ids, body, key=key), 202, "RemediationAccepted", "Immediate replay"
        )
        reordered = json.loads(
            json.dumps(
                {
                    "decisions": list(
                        reversed([dict(reversed(list(d.items()))) for d in body["decisions"]])
                    ),
                    "auditId": body["auditId"],
                }
            )
        )
        shuffled = ok(
            ctx,
            submit(ctx, ids, reordered, key=key),
            202,
            "RemediationAccepted",
            "Reordered replay",
        )
        if again != accepted or shuffled != accepted:
            raise CheckFailed(
                "A replay of the same key and body did not return the original 202 body."
            )
    if case == "conflict":
        changed = decision_body(ctx, fixture, audit["auditId"], plan["decisions"][1:])
        project_error(
            ctx,
            submit(ctx, ids, changed, key=key),
            409,
            "Same key with a different body",
            "IDEMPOTENCY_CONFLICT",
        )
    status = poll_remediation(ctx, accepted["remediationId"], "Keyed remediation")
    if status["status"] != "REMEDIATED":
        raise CheckFailed(f"The keyed remediation ended {status['status']}.")
    if case == "replay":
        late = ok(
            ctx,
            submit(ctx, ids, body, key=key),
            202,
            "RemediationAccepted",
            "Replay after completion",
        )
        if late != accepted:
            raise CheckFailed("A replay after completion did not return the original 202 body.")
    generated = [
        v
        for v in history(ctx, ids, "History after keyed requests")["versions"]
        if v["sourceVersionId"]
    ]
    if len(generated) != 1:
        raise CheckFailed(
            f"Keyed requests produced {len(generated)} generated versions; expected one."
        )
    return {
        "replay": "Pending, reordered and post-completion replays returned the original 202.",
        "conflict": "Same key, different body returned 409 IDEMPOTENCY_CONFLICT; one output.",
        "invalid_first": "A rejected request did not consume its key; the fixed body was accepted.",
    }[case]


# --- access matrix -----------------------------------------------------------------------------


def _access_base(ctx: SuiteContext, fixture: str) -> tuple[dict[str, str], Json]:
    return audited(ctx, fixture, f"access:{fixture}")


def access(ctx: SuiteContext) -> str:
    fixture = str(ctx.parameters["fixture"])
    resource, variant = str(ctx.parameters["resource"]), str(ctx.parameters["variant"])
    if resource in ("remediation", "output"):
        result = ctx.state.get("mixed_remediation")
        if result is None:
            raise Prerequisite("Not executed: no completed remediation exists to protect.")
        ids, audit = result["ids"], result["audit"]
        target = {
            "remediation": result["status"]["remediationId"],
            "output": result["status"]["outputId"],
        }
    else:
        ids, audit = _access_base(ctx, fixture)
        target = {}
    if variant == "unknown":
        ids = {
            "workbookId": "t9-unknown-workbook",
            "versionId": "t9-unknown-version",
            "auditId": "t9-unknown-audit",
        }
        target = {"remediation": "t9-unknown-remediation", "output": "t9-unknown-output"}
    identity: Identity | None = "userA"
    token: str | None = None
    session: SessionChoice = "main"
    expected = {
        "unauthenticated": 401,
        "invalid_token": 401,
        "foreign": 403,
        "unknown": 404,
        "wrong_namespace": 404,
        "reviewer_write_refused": 403,
        "reviewer_read": 200,
        "test_admin": 403,
    }[variant]
    if variant == "unauthenticated":
        identity = None
    elif variant == "invalid_token":
        identity, token = None, INVALID_TOKEN
    elif variant == "foreign":
        identity = "userB"
    elif variant in ("reviewer_write_refused", "reviewer_read"):
        identity = "reviewer"
    elif variant == "test_admin":
        identity = "test-admin"
    elif variant == "wrong_namespace":
        session = "none"
    action = f"{resource} as {variant}"
    binary = resource in ("output", "source")
    if resource == "new_version":
        response = call(
            ctx,
            "POST",
            f"/api/v1/workbooks/{ids['workbookId']}/versions",
            identity=identity,
            raw_token=token,
            session=session,
            files=multipart(ctx.suite.bytes_of(fixture), ctx.suite.accepted(fixture).upload_name),
        )
    elif resource == "reaudit":
        response = call(
            ctx,
            "POST",
            f"{version_path(ids)}/audits",
            identity=identity,
            raw_token=token,
            session=session,
            json={},
        )
    elif resource == "decisions":
        merge = _ids_for(ctx, fixture)[_first(ctx, fixture, "MERGED_CELLS", "MANUAL_REVIEW")]
        body = {
            "auditId": audit["auditId"] if variant != "unknown" else "t9-unknown-audit",
            "decisions": [{"findingId": merge, "action": "DECLINE"}],
        }
        response = submit(
            ctx, ids, body, key=new_key(), identity=identity, raw_token=token, session=session
        )
    else:
        path = {
            "remediation": f"/api/v1/remediations/{target.get('remediation')}",
            "output": f"/api/v1/outputs/{target.get('output')}/workbook",
            "history": history_path(ids),
            "source": f"{version_path(ids)}/source",
            "report": f"{audit_path(ids)}/report",
            "review": f"{audit_path(ids)}/review",
        }[resource]
        response = call(
            ctx, "GET", path, identity=identity, raw_token=token, session=session, binary=binary
        )
    if expected != 200:
        # Section 10 codes for new Phase 4 paths: 401 UNAUTHORIZED, 403 FORBIDDEN, 404 NOT_FOUND.
        code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND"}[expected]
        project_error(ctx, response, expected, action, code)
        return f"{action} returned {expected} with the project error envelope and no data."
    if response.status_code >= 500:
        expect_status(response, 200, action)
    ctx.qualify()
    expect_status(response, 200, action)
    if binary:
        if resource == "output" and response.content != ctx.state["mixed_remediation"]["output"]:
            raise CheckFailed("The reviewer's output download differs from the owner's.")
        if resource == "source" and response.content != ctx.suite.bytes_of(fixture):
            raise CheckFailed("The reviewer's source download differs from the uploaded bytes.")
    else:
        definition = {
            "remediation": "RemediationResponse",
            "history": "HistoryResponse",
            "report": "AuditReport",
            "review": "ReviewResponse",
        }[resource]
        body_of(response, definition, action)
    return f"{action} returned 200 with the owner's data; the grant is read-only."


# --- sessions and the report itself ------------------------------------------------------------


def session_coverage(ctx: SuiteContext) -> str:
    reads = []
    for attempt in (1, 2):
        response = ctx.client.request(
            "GET", "/test/report", identity="test-admin", params={"session_id": ctx.session_id}
        )
        reads.append(ok(ctx, response, 200, "SystemReport", f"System coverage read {attempt}"))
    first, second = reads
    if first["session_id"] != ctx.session_id or second != first:
        raise CheckFailed("Repeated system report reads differ or name another session.")
    from app.contracts.models import CoverageMeasure

    try:
        ctx.code = CoverageMeasure.model_validate(first["coverage"]["code"])
    except ValidationError:
        ctx.code = CoverageMeasure(
            covered=None,
            total=None,
            basis="Target code coverage unavailable: the system reported inconsistent counts",
        )
        raise CheckFailed(
            "System coverage counts are inconsistent (covered exceeds total)."
        ) from None
    return "Two system report reads were identical and scoped to this session."


def session_cleanup(ctx: SuiteContext) -> str:
    """Clean only this case's own inactive scratch namespace.

    The fault-session namespaces stay intact as run evidence (section 14.1); the tester
    stores their IDs with the run and cleans them on its next setup.
    """
    fixture = str(ctx.parameters["fixture"])
    with setup(ctx):
        scratch, response = setup_session(ctx, {})
        expect_status(response, 201, "Scratch session setup")
        ids = upload(ctx, fixture, "Scratch upload", session=scratch)
        poll_audit(ctx, ids, "Scratch audit", session=scratch)
    carrier, response = setup_session(ctx, {"cleanup_session_ids": [scratch]})
    ctx.qualify()
    expect_status(response, 201, "Cleanup carrier setup")
    gone = call(ctx, "GET", version_path(ids), session=scratch)
    project_error(ctx, gone, 400, "Cleaned scratch session")
    kept = ctx.state.get("p4:upload:seeded_all_six")
    if kept is not None:
        version(ctx, kept, "Main session data after cleanup")
    fault = ctx.state.get("fault")
    if fault is not None:
        version(
            ctx,
            fault["ids"],
            "Fault-session evidence after cleanup",
            session=ctx.state["fault_session"],
        )
    return (
        f"Cleanup carrier {carrier} removed scratch session {scratch}; main-session data and "
        "fault-session evidence remain readable."
    )


def report_format(ctx: SuiteContext) -> str:
    planned = [test.test_id for test in ctx.suite.manifest.tests]
    seen = [result.test_id for result in ctx.results]
    ctx.qualify()
    if seen != planned[: len(seen)]:
        raise CheckFailed("Recorded results are not in manifest order.")
    for result in ctx.results:
        problems = contract.violations("TestResult", result.model_dump(mode="json"))
        if problems:
            raise CheckFailed(f"Result {result.test_id} violates TestResult: {problems[0]}.")
    return f"{len(seen)} recorded results are ordered, unique and schema-valid so far."


CHECKS: dict[str, Callable[[SuiteContext], str]] = {
    "fault_setup": fault_setup,
    "fault_envelope": fault_envelope,
    "fault_recovery": fault_recovery,
    "fault_auth": fault_auth,
    "fault_invalid": fault_invalid,
    "fixture_upload": fixture_upload,
    "fixture_audit": fixture_audit,
    "fixture_upload_audit": fixture_upload_audit,
    "rule_vector": rule_vector,
    "review_context": review_context,
    "review_reviewer": review_reviewer,
    "version_new": version_new,
    "version_same_bytes": version_same_bytes,
    "reaudit": reaudit,
    "reaudit_invalid": reaudit_invalid,
    "reaudit_determinism": reaudit_determinism,
    "source_before": source_before,
    "decisions_mixed": decisions_mixed,
    "apply_correction": apply_correction,
    "apply_no_edits": apply_no_edits,
    "reject_decisions": reject_decisions,
    "batch_atomic": batch_atomic,
    "idempotency": idempotency,
    "output_hash": output_hash,
    "source_after": source_after,
    "preservation": preservation,
    "output_audit": output_audit,
    "report_complete": report_complete,
    "report_stable": report_stable,
    "history": history_check,
    "remediated_status": remediated_status,
    "access": access,
    "download_large": download_large,
    "session_coverage": session_coverage,
    "session_cleanup": session_cleanup,
    "report_format": report_format,
}
