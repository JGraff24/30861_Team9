"""Track accepted Phase4 work so sequential cases do not accidentally test overload.

The observer sees completed HTTP responses before a case checks its expected status. In
particular, an incorrectly accepted negative request stays a failure, but its durable work
is allowed to finish before an unrelated fixture is submitted. Only bounded owner GETs are
used; no mutation is retried, cancelled, or silently replaced by a fresh fixture.
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
    poll_path: str
    accepted_at: float


class AsyncWorkTracker:
    def __init__(self, ctx: "SuiteContext") -> None:
        self.ctx = ctx
        self.pending: dict[Key, AcceptedOperation] = {}

    def observe(
        self, method: str, path: str, namespace: str | None, response: httpx.Response
    ) -> None:
        if method != "POST" or response.status_code != 202 or len(response.content) > 16_384:
            return
        parts = path.strip("/").split("/")
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
        if kind == "remediation":
            poll_path = f"/api/v1/remediations/{resource_id}"
        else:
            workbook = body["workbookId"] if definition == "UploadResponse" else parts[3]
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
            ),
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
        while True:
            if ctx.client.remaining() <= 0:
                raise DeadlineExceeded("suite deadline reached while draining accepted work")
            left = deadline - ctx.clock()
            if left <= 0:
                raise SystemFault(
                    f"Accepted {operation.kind} {operation.resource_id} did not finish "
                    f"within its {seconds}s acceptance budget."
                )
            response = ctx.client.request(
                "GET",
                operation.poll_path,
                identity="userA",
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


def tracker_for(ctx: "SuiteContext") -> AsyncWorkTracker:
    tracker = ctx.state.get("_accepted_async_work_tracker")
    if not isinstance(tracker, AsyncWorkTracker):
        tracker = AsyncWorkTracker(ctx)
        ctx.state["_accepted_async_work_tracker"] = tracker
        ctx.client.response_observer = tracker.observe
    return tracker
