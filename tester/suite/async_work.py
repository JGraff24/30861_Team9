"""Track accepted Phase4 work so sequential cases do not accidentally test overload.

The observer sees completed HTTP responses before a case checks its expected status. In
particular, an incorrectly accepted negative request stays a failure, but its durable work
is allowed to finish before an unrelated fixture is submitted. Only bounded owner GETs are
used; no mutation is retried, cancelled, or silently replaced by a fresh fixture.

Phase 5 adds audits that a clock advance accepted (ClockResponse.triggeredAudits). The clock
response carries only audit IDs, so the drain resolves each one through the owner's history of
the workbooks already seen in that namespace, then polls it like any other accepted audit.
Owner identity is remembered per workbook so a userB workbook is drained as userB.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx

from tester.phase4 import contract
from tester.target import CommunicationError, DeadlineExceeded

if TYPE_CHECKING:
    from tester.suite.checks import SuiteContext

logger = logging.getLogger(__name__)
Kind = Literal["audit", "remediation"]
Key = tuple[str | None, Kind, str]


def error_summary(error: object) -> str:
    """Keep job correlation and the declared stage, without retaining arbitrary details."""
    if not isinstance(error, dict):
        return "missing error"
    code = str(error.get("code", "unknown"))[:64]
    request_id = str(error.get("requestId", "unknown"))[:64]
    details = error.get("details")
    stage = details.get("stage") if isinstance(details, dict) else None
    if not isinstance(stage, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,40}", stage) is None:
        stage = "unspecified"
    return f"{code}; requestId={request_id}; stage={stage}"


@dataclass(frozen=True)
class AcceptedOperation:
    kind: Kind
    resource_id: str
    namespace: str | None
    poll_path: str | None  # None: a clock-accepted audit not yet resolved to its version
    accepted_at: float
    identity: str = "userA"
    workbook: str | None = None


class AsyncWorkTracker:
    def __init__(self, ctx: "SuiteContext") -> None:
        self.ctx = ctx
        self.pending: dict[Key, AcceptedOperation] = {}
        # namespace -> workbookId -> owner identity, from accepted uploads.
        self.workbooks: dict[str | None, dict[str, str]] = {}

    def owner(self, namespace: str | None, workbook_id: str) -> str:
        return self.workbooks.get(namespace, {}).get(workbook_id, "userA")

    def observe(
        self,
        method: str,
        path: str,
        namespace: str | None,
        identity: str | None,
        response: httpx.Response,
    ) -> None:
        parts = path.strip("/").split("/")
        if method == "DELETE" and response.status_code in (204, 404) and len(parts) == 4:
            # A completed owner deletion removed this workbook's work with it; there is
            # nothing left to drain (and polling would only observe the 404).
            self.workbooks.get(namespace, {}).pop(parts[3], None)
            if response.status_code == 204:
                for pending_key, operation in list(self.pending.items()):
                    if operation.namespace == namespace and operation.workbook == parts[3]:
                        del self.pending[pending_key]
            return
        if method == "POST" and parts == ["api", "v1", "test", "clock"]:
            self._observe_clock(namespace, response)
            return
        if method != "POST" or response.status_code != 202 or len(response.content) > 16_384:
            return
        user = identity if identity in ("userA", "userB") else "userA"
        if parts[:3] != ["api", "v1", "workbooks"]:
            return
        definition = None
        if len(parts) == 3 or (len(parts) == 5 and parts[4] == "versions"):
            definition = "UploadResponse"
        elif len(parts) == 7 and parts[4] == "versions":
            definition = {"audits": "ReauditResponse", "remediations": "RemediationAccepted"}.get(
                parts[6]
            )
        if definition is None:
            return
        try:
            body = contract.parse_json(response.content, definition)
        except contract.ContractViolation:
            # An invalid acceptance has no trustworthy polling identifiers. The case's
            # normal schema/status assertion still reports the defect; never invent IDs.
            return
        kind: Kind = "remediation" if definition == "RemediationAccepted" else "audit"
        resource_id = body["remediationId"] if kind == "remediation" else body["auditId"]
        workbook = body["workbookId"] if definition == "UploadResponse" else parts[3]
        if definition == "UploadResponse":
            self.workbooks.setdefault(namespace, {}).setdefault(workbook, user)
        owner = self.owner(namespace, workbook)
        if kind == "remediation":
            poll_path = f"/api/v1/remediations/{resource_id}"
        else:
            poll_path = (
                f"/api/v1/workbooks/{workbook}/versions/{body['versionId']}/audits/{resource_id}"
            )
        key: Key = (namespace, kind, resource_id)
        # Identical idempotency replays must not extend an already accepted operation's budget.
        self.pending.setdefault(
            key,
            AcceptedOperation(
                kind,
                resource_id,
                namespace,
                poll_path,
                self.ctx.clock(),
                identity=owner,
                workbook=workbook,
            ),
        )

    def _observe_clock(self, namespace: str | None, response: httpx.Response) -> None:
        if response.status_code != 200 or len(response.content) > 1_048_576:
            return
        try:
            from tester.phase4.contract import use_pack

            with use_pack("phase5"):
                body = contract.parse_json(response.content, "ClockResponse")
        except contract.ContractViolation:
            return  # the case's own schema assertion reports the defect
        for audit_id in body["triggeredAudits"]:
            self.pending.setdefault(
                (namespace, "audit", audit_id),
                AcceptedOperation("audit", audit_id, namespace, None, self.ctx.clock()),
            )

    def complete(self, kind: Kind, resource_id: str, namespace: str | None) -> None:
        self.pending.pop((namespace, kind, resource_id), None)

    def deadline(
        self, kind: Kind, resource_id: str, namespace: str | None, seconds: float
    ) -> float:
        operation = self.pending.get((namespace, kind, resource_id))
        return (operation.accepted_at if operation else self.ctx.clock()) + seconds

    def drain(self) -> None:
        from tester.suite.checks import SystemFault

        ctx = self.ctx
        ctx.setup_depth += 1
        failure: SystemFault | CommunicationError | None = None
        try:
            for key, operation in list(self.pending.items()):
                try:
                    self._drain_one(operation)
                except (SystemFault, CommunicationError) as exc:
                    if failure is None:
                        failure = exc
                finally:
                    # A failed/unreachable operation is diagnosed once, not polled anew
                    # for every subsequent case. Independent assertions may still run.
                    self.pending.pop(key, None)
        finally:
            ctx.setup_depth -= 1
        if failure is not None:
            raise failure

    def _drain_one(self, operation: AcceptedOperation) -> None:
        from tester.suite.checks import SystemFault

        ctx, limits = self.ctx, self.ctx.suite.manifest.limits
        assert limits is not None
        seconds = (
            limits.audit_poll_seconds
            if operation.kind == "audit"
            else limits.remediation_poll_seconds
        )
        deadline = operation.accepted_at + seconds
        if operation.poll_path is None:
            operation = self._resolve(operation, deadline)
        while True:
            if ctx.client.remaining() <= 0:
                raise DeadlineExceeded("suite deadline reached while draining accepted work")
            left = deadline - ctx.clock()
            if left <= 0:
                raise SystemFault(
                    f"Accepted {operation.kind} {operation.resource_id} did not finish "
                    f"within its {seconds}s acceptance budget."
                )
            assert operation.poll_path is not None
            response = ctx.client.request(
                "GET",
                operation.poll_path,
                identity=operation.identity,  # type: ignore[arg-type]
                session_header=False,
                headers={"X-Test-Session": operation.namespace} if operation.namespace else {},
                timeout=min(limits.business_get_seconds, left),
                max_bytes=limits.json_response_bytes,
            )
            if response.status_code != 200:
                raise SystemFault(
                    f"Accepted {operation.kind} {operation.resource_id} could not be drained "
                    f"(HTTP {response.status_code})."
                )
            definition = "AuditResponse" if operation.kind == "audit" else "RemediationResponse"
            try:
                body = contract.parse_json(response.content, definition)
            except contract.ContractViolation:
                raise SystemFault(
                    f"Accepted {operation.kind} returned an invalid polling envelope."
                ) from None
            expected_id = "auditId" if operation.kind == "audit" else "remediationId"
            if body[expected_id] != operation.resource_id:
                raise SystemFault("Accepted work polling returned a different resource ID.")
            processing = "PROCESSING" if operation.kind == "audit" else "REMEDIATING"
            if body["status"] != processing:
                if body["status"] == "PROCESSING_FAILED":
                    raise SystemFault(
                        f"Accepted {operation.kind} {operation.resource_id} ended "
                        f"PROCESSING_FAILED ({error_summary(body['error'])})."
                    )
                return
            if ctx.client.remaining() < 1:
                raise DeadlineExceeded("suite deadline reached while draining accepted work")
            ctx.sleep(min(1.0, max(0.0, deadline - ctx.clock())))

    def _resolve(self, operation: AcceptedOperation, deadline: float) -> AcceptedOperation:
        """Find a clock-accepted audit's version through owner history GETs only."""
        from tester.suite.checks import SystemFault

        ctx, limits = self.ctx, self.ctx.suite.manifest.limits
        assert limits is not None
        for workbook, owner in list(self.workbooks.get(operation.namespace, {}).items()):
            left = deadline - ctx.clock()
            if left <= 0:
                break
            response = ctx.client.request(
                "GET",
                f"/api/v1/workbooks/{workbook}/history",
                identity=owner,  # type: ignore[arg-type]
                session_header=False,
                headers={"X-Test-Session": operation.namespace} if operation.namespace else {},
                timeout=min(limits.business_get_seconds, left),
                max_bytes=limits.json_response_bytes,
            )
            if response.status_code != 200:
                continue
            try:
                body = contract.parse_json(response.content, "HistoryResponse")
            except contract.ContractViolation:
                continue
            for audit in body["audits"]:
                if audit["auditId"] == operation.resource_id:
                    return AcceptedOperation(
                        "audit",
                        operation.resource_id,
                        operation.namespace,
                        f"/api/v1/workbooks/{workbook}/versions/{audit['versionId']}"
                        f"/audits/{operation.resource_id}",
                        operation.accepted_at,
                        identity=owner,
                        workbook=workbook,
                    )
        raise SystemFault(
            f"Clock-accepted audit {operation.resource_id} is not visible in any known "
            "workbook history, so it could not be drained."
        )


def tracker_for(ctx: "SuiteContext") -> AsyncWorkTracker:
    tracker = ctx.state.get("_accepted_async_work_tracker")
    if not isinstance(tracker, AsyncWorkTracker):
        tracker = AsyncWorkTracker(ctx)
        ctx.state["_accepted_async_work_tracker"] = tracker
        ctx.client.response_observer = tracker.observe
    return tracker
