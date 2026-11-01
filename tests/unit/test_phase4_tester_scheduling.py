"""Focused tester scheduling regressions; the fake target models a shared durable worker."""

import httpx
import pytest

from app.config import AppEnvironment
from tester.config import TargetAuth
from tester.phase4 import checks
from tester.suite import checks as legacy
from tester.suite.checks import SuiteContext
from tester.suite.manifest import load_suite
from tester.suite.registry import names
from tester.suite.runner import run_one
from tester.target import TargetClient
from tests.support.fake_phase4 import BASE_URL, TOKENS, FakePhase4
from tests.support.fake_system import address_resolver
from tests.unit.test_phase4_tester import MANIFEST, SMALL, run_phase4


@pytest.fixture(autouse=True)
def reduced_boundaries(monkeypatch):
    monkeypatch.setattr(legacy, "UPLOAD_LIMIT_BYTES", SMALL["upload_limit"])
    monkeypatch.setattr(legacy, "WORKSHEET_LIMIT", SMALL["worksheet_limit"])
    monkeypatch.setattr(legacy, "NONEMPTY_CELL_LIMIT", SMALL["cell_limit"])


class UnexpectedReviewerAcceptance(FakePhase4):
    """Wrongly authorizes one reviewer write; its accepted job holds the shared worker.

    Polling this operation represents allowing the worker to finish. The double deliberately
    fails independent source audits submitted while it is still busy, like a queue expiry.
    This is tester-control evidence, not an implementation of production audit semantics.
    """

    def __init__(self):
        super().__init__(**SMALL)
        self.busy_remediations = set()
        self.bad_acceptances = 0

    def route(self, request):
        if (
            request.method == "POST"
            and request.url.path.endswith("/remediations")
            and self.subject(request) == "reviewer"
            and not self.bad_acceptances
        ):
            # Deliberately simulate an authorization defect without changing tester oracles.
            headers = dict(request.headers)
            headers["authorization"] = "Bearer " + TOKENS["userA"]
            forwarded = httpx.Request(
                request.method, request.url, headers=headers, content=request.content
            )
            response = super().route(forwarded)
            if response.status_code == 202:
                self.busy_remediations.add(response.json()["remediationId"])
                self.bad_acceptances += 1
            return response
        return super().route(request)

    def remediation_status(self, remediation_id, subject, namespace):
        response = super().remediation_status(remediation_id, subject, namespace)
        if response.json()["status"] != "REMEDIATING":
            self.busy_remediations.discard(remediation_id)
        return response

    def finish(self, audit):
        version = self.versions[audit.version_id]
        queued_behind_reviewer_work = bool(self.busy_remediations) and not version.remediation_id
        if queued_behind_reviewer_work:
            audit.review = [{"fault": True}]
        super().finish(audit)
        if queued_behind_reviewer_work:
            audit.error["code"] = "PROCESSING_TIMEOUT"
            audit.error["message"] = "Processing deadline expired."
            audit.error["details"] = {"stage": "queue"}
            audit.report = self.build_report(audit, version)


def test_unexpected_reviewer_acceptance_remains_failed_without_starving_later_cases(tmp_path):
    target = UnexpectedReviewerAcceptance()
    report = run_phase4(tmp_path, target)
    results = {item["test_id"]: item for item in report["results"]}
    assert results["P4-REVIEW-REVIEWER"]["outcome"] == "fail"
    assert "202" in results["P4-REVIEW-REVIEWER"]["message"]
    assert results["P4-REAUDIT-INVALID"]["outcome"] == "pass", results["P4-REAUDIT-INVALID"]
    assert results["P4-REAUDIT-INVALID--UNKNOWN-FIELD"]["outcome"] == "pass", results[
        "P4-REAUDIT-INVALID--UNKNOWN-FIELD"
    ]
    assert target.bad_acceptances == 1  # No replay/re-upload is used to hide the defect.
    assert not target.busy_remediations


class UnexpectedAuditAcceptance(FakePhase4):
    def __init__(self, variant):
        super().__init__(**SMALL)
        self.variant = variant
        self.busy_audit = None
        self.bad_acceptances = 0

    def route(self, request):
        headers = dict(request.headers)
        content = request.content
        wrong_reason = (
            self.variant == "invalid_reaudit"
            and request.url.path.endswith("/audits")
            and request.method == "POST"
            and b"SCHEDULED" in content
        )
        wrong_role = (
            self.variant == "reviewer_version"
            and request.url.path.endswith("/versions")
            and request.method == "POST"
            and self.subject(request) == "reviewer"
        )
        if not self.bad_acceptances and (wrong_reason or wrong_role):
            if wrong_reason:
                content = b"{}"
                headers["content-length"] = "2"
            else:
                headers["authorization"] = "Bearer " + TOKENS["userA"]
            response = super().route(
                httpx.Request(request.method, request.url, headers=headers, content=content)
            )
            if response.status_code == 202:
                self.busy_audit = response.json()["auditId"]
                self.bad_acceptances += 1
            return response
        return super().route(request)

    def finish(self, audit):
        queued = self.busy_audit and audit.audit_id != self.busy_audit
        if queued:
            audit.review = [{"fault": True}]
        super().finish(audit)
        if queued:
            audit.error["code"] = "PROCESSING_TIMEOUT"
            audit.error["details"] = {"stage": "queue"}
            audit.report = self.build_report(audit, self.versions[audit.version_id])
        if audit.audit_id == self.busy_audit:
            self.busy_audit = None


@pytest.mark.parametrize(
    "variant,original,next_case",
    [
        ("invalid_reaudit", "P4-REAUDIT-INVALID", "P4-REAUDIT-INVALID--UNKNOWN-FIELD"),
        ("reviewer_version", "P4-ACCESS-NEW-VERSION-REVIEWER-WRITE-REFUSED", "P4-DOWNLOAD-LARGE"),
    ],
)
def test_unexpected_audit_acceptance_is_drained_without_reclassifying_original_failure(
    tmp_path,
    variant,
    original,
    next_case,
):
    target = UnexpectedAuditAcceptance(variant)
    report = run_phase4(tmp_path, target)
    results = {item["test_id"]: item for item in report["results"]}
    assert results[original]["outcome"] == "fail" and "202" in results[original]["message"]
    assert results[next_case]["outcome"] == "pass", results[next_case]
    assert target.bad_acceptances == 1 and target.busy_audit is None


def test_failed_background_acceptance_does_not_hide_original_authorization_failure(tmp_path):
    class FailedReviewerWork(UnexpectedReviewerAcceptance):
        def complete(self, remediation, workbook):
            if remediation.remediation_id in self.busy_remediations:
                remediation.status = "PROCESSING_FAILED"
                remediation.error = {
                    "code": "PRESERVATION_FAILED",
                    "message": "Preservation failed.",
                    "requestId": "failed-reviewer-job",
                    "details": {"stage": "preservation"},
                }
            else:
                super().complete(remediation, workbook)

    target = FailedReviewerWork()
    report = run_phase4(tmp_path, target)
    results = {item["test_id"]: item for item in report["results"]}
    original = results["P4-REVIEW-REVIEWER"]
    assert original["outcome"] == "fail" and "source" not in original
    assert "202" in original["message"] and "PRESERVATION_FAILED" in original["message"]
    assert "stage=preservation" in original["message"]
    assert results["P4-REAUDIT-INVALID"]["outcome"] == "pass"


def isolated_context(target):
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

        def sleep(self, seconds):
            self.value += seconds

    clock = Clock()
    suite = load_suite(MANIFEST, names())
    target.sessions["isolated"] = {"faults": 0}
    client = TargetClient(
        BASE_URL,
        TargetAuth(**TOKENS),
        AppEnvironment.DEVELOPMENT,
        1000,
        transport=target.transport(),
        resolver=address_resolver(),
        clock=clock,
    )
    client.session_id = "isolated"
    ctx = SuiteContext(
        client=client, suite=suite, session_id="isolated", sleep=clock.sleep, clock=clock
    )
    test = next(test for test in suite.manifest.tests if test.test_id == "P4-CLEAN-ALL-SIX")
    return ctx, test, clock


def test_a_genuinely_failed_clean_audit_is_not_retried_or_replaced():
    target = FakePhase4(**SMALL, fail_audits_for={"clean_all_six.xlsx"})
    ctx, test, _ = isolated_context(target)
    result = run_one(ctx, test)
    assert result.outcome == "fail"
    assert "TEST_AUDIT_FAILURE" in result.message and "stage=audit" in result.message
    assert "requestId=job-" in result.message and "auditId=" in result.message
    assert len([request for request in target.requests if request.method == "POST"]) == 1
    assert len([request for request in target.requests if request.method == "GET"]) == 2


def test_pending_work_keeps_original_acceptance_deadline(monkeypatch):
    target = FakePhase4(**SMALL)
    ctx, test, clock = isolated_context(target)

    def accept_then_wait(ctx):
        checks.upload(ctx, "clean_all_six", "Accepted once")
        ctx.sleep(79)
        return "The acceptance was valid."

    monkeypatch.setitem(checks.CHECKS, "accept_then_wait", accept_then_wait)
    result = run_one(ctx, test.model_copy(update={"check": "accept_then_wait"}))
    assert result.outcome == "error" and result.source == "system"
    assert "80s acceptance budget" in result.message
    assert clock.value == 80
    assert len([request for request in target.requests if request.method == "POST"]) == 1
