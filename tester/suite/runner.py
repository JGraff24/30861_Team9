"""Run planned tests in manifest order and build the instructor SuiteReport."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.contracts.models import (
    CoverageMeasure,
    ErrorSource,
    Outcome,
    SuiteCoverage,
    SuiteReport,
    TestResult,
)
from tester.suite.checks import (
    CheckFailed,
    Prerequisite,
    SuiteContext,
    SystemFault,
)
from tester.suite.manifest import FixturePending, LoadedSuite, PlannedTest
from tester.suite.registry import handler
from tester.target import CommunicationError, DeadlineExceeded

logger = logging.getLogger(__name__)

COMPONENT_BASIS = (
    "Phase 3 component catalog C1-C6 in tests/fixtures/manifest.json; a component counts when "
    "a recorded qualifying call/assertion executes, regardless of its final outcome."
)
SYSTEM_BASIS = (
    "Phase 3 portions of AC-01/02/03/04/05/14/15/18 in tests/fixtures/manifest.json; an AC "
    "counts only when its predeclared assertion executes. This measures partial scope; "
    "AC-14 requires an observed controlled failure, not ordinary corrupt-input rejection."
)
PHASE4_COMPONENT_BASIS = (
    "Phase 4 component catalog C1-C9 (specification 2.1.0 section 16); a component counts when a "
    "recorded qualifying assertion executes, even if it fails; skipped paths earn nothing. This "
    "is an HTTP-only run: C9 review UI needs retained browser/manual evidence and is never "
    "counted here, so covered is at most 8."
)
PHASE4_SYSTEM_BASIS = (
    "Exercised-criterion coverage of AC-01 to AC-11 (denominator 11): an AC counts when at least "
    "one mapped qualifying assertion executed, even if it failed. This is not acceptance "
    "success. AC-09 HTTP checks are only the automated preservation portion; Microsoft Excel "
    "no-repair inspection remains a separate human release gate."
)
PHASE5_COMPONENT_BASIS = (
    "Shared component catalog C1-C12 (specification 3.0.0 section 15) in the frozen suite "
    "manifest; a component counts once when a test executed one of its predeclared qualifying "
    "assertions in this session, even if it failed. Setup, preparation, accepted-work draining "
    "and skipped tests earn nothing. HTTP-only run: C9 user interface needs retained browser/"
    "manual evidence and is never counted here, so covered is at most 11."
)
PHASE5_SYSTEM_BASIS = (
    "Shared acceptance criteria AC-01 to AC-18 (denominator 18, specification 3.0.0 section 17): "
    "a criterion counts once when at least one mapped qualifying assertion executed, even if it "
    "failed. This is evaluated coverage, not acceptance. HTTP-only run: AC-17 needs browser/"
    "manual accessibility evidence and is never counted here; AC-09 counts only the automated "
    "preservation portion (Excel no-repair inspection is separate); AC-16 counts HTTP deletion "
    "evidence only (HTTPS, credential protection and backup/restore evidence are manual); AC-18 "
    "counts tester-observed real-time runs (target-side queue/engine timing is separate)."
)
CODE_UNAVAILABLE = "Target code coverage unavailable: {reason}"
# Neither component C9 nor AC-17 can be exercised by an HTTP-only run (section 14).
HTTP_EXCLUDED = {"C9", "AC-17"}


@dataclass(frozen=True)
class RunOutcome:
    report: SuiteReport
    aborted: bool
    abort_message: str | None = None


def build_report(
    suite: LoadedSuite,
    session_id: str,
    target_url: str,
    results: list[TestResult],
    code: CoverageMeasure,
    markers: dict[str, dict[str, list[str]]] | None = None,
) -> SuiteReport:
    planned = {test.test_id: test for test in suite.manifest.tests}
    # Skipped paths earn no coverage, even if a marker was recorded before the skip.
    skipped = {result.test_id for result in results if result.outcome == Outcome.SKIP}
    reported = {result.test_id for result in results}
    components: set[str] = set()
    acceptance: set[str] = set()
    executed: set[str] = set()
    for test_id, evidence in (markers or {}).items():
        test = planned.get(test_id)
        if test is not None and test_id not in skipped and test_id in reported:
            executed.add(test_id)
            components.update(set(evidence["components"]) & set(test.components))
            acceptance.update(set(evidence["acceptance"]) & set(test.acceptance))
    if suite.manifest.phase == "5":
        return _phase5_report(
            suite, session_id, target_url, results, code, components, acceptance, executed
        )
    phase4 = suite.manifest.phase == "4"
    if phase4:
        components.discard("C9")  # HTTP-only runs never qualify the review UI (section 16).
    coverage = SuiteCoverage(
        code=code,
        component=CoverageMeasure(
            covered=len(components),
            total=len(suite.manifest.components),
            basis=PHASE4_COMPONENT_BASIS if phase4 else COMPONENT_BASIS,
        ),
        system=CoverageMeasure(
            covered=len(acceptance),
            total=len(suite.manifest.acceptance),
            basis=PHASE4_SYSTEM_BASIS if phase4 else SYSTEM_BASIS,
        ),
    )
    return SuiteReport(
        session_id=session_id, target_url=target_url, results=results, coverage=coverage
    )


def _phase5_report(
    suite: LoadedSuite,
    session_id: str,
    target_url: str,
    results: list[TestResult],
    code: CoverageMeasure,
    components: set[str],
    acceptance: set[str],
    executed: set[str],
) -> SuiteReport:
    """Conditional own/cross profile: the frozen profile alone decides coverage.model."""
    from tester.phase5.profile import RunProfile, model_coverage

    if suite.profile is None:
        raise RuntimeError("Phase 5 plan has no frozen profile")
    profile = RunProfile.restore(dict(suite.profile))
    model = model_coverage(profile, executed) if profile.kind == "own" else None
    coverage = SuiteCoverage(
        code=code,
        component=CoverageMeasure(
            covered=len(components - HTTP_EXCLUDED),
            total=len(suite.manifest.components),
            basis=PHASE5_COMPONENT_BASIS,
        ),
        system=CoverageMeasure(
            covered=len(acceptance - HTTP_EXCLUDED),
            total=len(suite.manifest.acceptance),
            basis=PHASE5_SYSTEM_BASIS,
        ),
        # Cross-team reports omit the key entirely (never null or a placeholder).
        **({"model": model} if model is not None else {}),
    )
    return SuiteReport(
        session_id=session_id, target_url=target_url, results=results, coverage=coverage
    )


def retrieve_code_coverage(ctx: SuiteContext) -> CoverageMeasure:
    """Copy the target's measured code coverage after the last planned project call.

    The tester never computes target execution itself. Any refusal, invalid body, foreign
    session or communication failure yields null counts with the reason (section 13/14).
    """
    from tester.phase4 import contract

    limits = ctx.suite.manifest.limits
    try:
        response = ctx.client.request(
            "GET",
            "/test/report",
            identity="test-admin",
            params={"session_id": ctx.session_id},
            timeout=limits.business_get_seconds if limits is not None else 10,
        )
    except CommunicationError as exc:
        return CoverageMeasure(
            covered=None, total=None, basis=CODE_UNAVAILABLE.format(reason=f"no response ({exc})")
        )
    if response.status_code != 200:
        return CoverageMeasure(
            covered=None,
            total=None,
            basis=CODE_UNAVAILABLE.format(
                reason=f"final system report returned HTTP {response.status_code}"
            ),
        )
    try:
        body = contract.parse_json(response.content, "SystemReport")
        if body["session_id"] != ctx.session_id:
            raise contract.ContractViolation("system report names a different session")
        return CoverageMeasure.model_validate(body["coverage"]["code"])
    except (contract.ContractViolation, ValueError):
        return CoverageMeasure(
            covered=None,
            total=None,
            basis=CODE_UNAVAILABLE.format(reason="final system report was invalid or foreign"),
        )


def interrupted_results(
    suite: LoadedSuite, completed: list[TestResult], message: str
) -> list[TestResult]:
    """Keep completed outcomes, mark the interrupted test tester error, skip the rest."""
    results = list(completed)
    completed_ids = {result.test_id for result in completed}
    remaining = [test for test in suite.manifest.tests if test.test_id not in completed_ids]
    for index, test in enumerate(remaining):
        if index == 0:
            results.append(
                TestResult(
                    test_id=test.test_id,
                    outcome=Outcome.ERROR,
                    source=ErrorSource.TESTER,
                    message=message,
                )
            )
        else:
            results.append(
                TestResult(
                    test_id=test.test_id,
                    outcome=Outcome.SKIP,
                    message="Not executed: the tester aborted the suite.",
                )
            )
    return results


def _run_one_assertion(ctx: SuiteContext, test: PlannedTest) -> TestResult:
    def result(outcome: Outcome, message: str, source: ErrorSource | None = None) -> TestResult:
        return TestResult(test_id=test.test_id, outcome=outcome, message=message, source=source)

    ctx.current = test
    if test.phase == "future":
        return result(Outcome.SKIP, f"Planned for a later phase: {test.description}")
    try:
        return result(Outcome.PASS, handler(test.check)(ctx))
    except FixturePending as exc:
        return result(Outcome.SKIP, str(exc))
    except CheckFailed as exc:
        return result(Outcome.FAIL, str(exc))
    except Prerequisite as exc:
        return result(Outcome.SKIP, str(exc))
    except SystemFault as exc:
        return result(Outcome.ERROR, str(exc), ErrorSource.SYSTEM)
    except CommunicationError as exc:
        if test.check == "system_report":
            ctx.code = CoverageMeasure(
                covered=None, total=None, basis=f"Target code coverage unavailable: {exc}"
            )
        return result(Outcome.ERROR, f"No usable response: {exc}.", ErrorSource.COMMUNICATION)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        # Phase 3 keeps its documented abort-on-defect behavior. In Phase 4 one case's
        # unexpected tester problem is that case's error, and independent cases continue
        # (specification 2.1.0 section 14: error means evaluation could not proceed).
        if test.phase not in ("4", "5"):
            raise
        logger.error("Phase 4 check %s could not be evaluated: %s", test.check, type(exc).__name__)
        return result(
            Outcome.ERROR,
            f"The tester could not evaluate this case ({type(exc).__name__}).",
            ErrorSource.TESTER,
        )


def run_one(ctx: SuiteContext, test: PlannedTest) -> TestResult:
    if not ctx.suite.manifest.integrated:
        return _run_one_assertion(ctx, test)
    from tester.suite.async_work import tracker_for

    tracker = tracker_for(ctx)
    result = _run_one_assertion(ctx, test)
    try:
        tracker.drain()
    except DeadlineExceeded:
        raise
    except (SystemFault, CommunicationError) as exc:
        diagnostic = f"Background acceptance: {exc}"
        logger.warning("accepted_work_drain_failed case=%s: %s", test.test_id, diagnostic)
        if result.outcome == Outcome.PASS:
            return TestResult(
                test_id=test.test_id,
                outcome=Outcome.ERROR,
                source=ErrorSource.COMMUNICATION
                if isinstance(exc, CommunicationError)
                else ErrorSource.SYSTEM,
                message=diagnostic,
            )
        # Preserve the original rejected-status/authorization finding and its source rule.
        return result.model_copy(update={"message": result.message + " " + diagnostic})
    return result


def run_suite(
    ctx: SuiteContext,
    target_url: str,
    progress: Callable[[list[TestResult], dict[str, dict[str, list[str]]]], None],
) -> RunOutcome:
    if ctx.suite.manifest.phase == "5":
        from tester.phase4 import contract

        # Inherited Phase 4 checks validate new reports against specification 3.0.0.
        with contract.use_pack("phase5"):
            return _run_suite(ctx, target_url, progress)
    return _run_suite(ctx, target_url, progress)


def _run_suite(
    ctx: SuiteContext,
    target_url: str,
    progress: Callable[[list[TestResult], dict[str, dict[str, list[str]]]], None],
) -> RunOutcome:
    results: list[TestResult] = []
    ctx.results = results
    for test in ctx.suite.manifest.tests:
        try:
            if ctx.client.remaining() <= 0:
                raise DeadlineExceeded("suite deadline reached before the test")
            result = run_one(ctx, test)
            if ctx.client.remaining() <= 0:
                raise DeadlineExceeded("suite deadline reached while evaluating the test")
            results.append(result)
        except DeadlineExceeded:
            message = "The tester's suite deadline was reached during this test."
            final = interrupted_results(ctx.suite, results, message)
            code = CoverageMeasure(
                covered=None,
                total=None,
                basis=CODE_UNAVAILABLE.format(reason="suite deadline reached before retrieval"),
            )
            progress(final, ctx.markers)
            report = build_report(ctx.suite, ctx.session_id, target_url, final, code, ctx.markers)
            return RunOutcome(
                report, aborted=True, abort_message="Tester aborted: suite deadline reached."
            )
        progress(results, ctx.markers)
    if ctx.suite.manifest.phase == "5":
        # Instructor order: execute every planned test, then retrieve code coverage.
        try:
            if ctx.client.remaining() <= 0:
                raise DeadlineExceeded("suite deadline reached before coverage retrieval")
            ctx.code = retrieve_code_coverage(ctx)
        except DeadlineExceeded:
            ctx.code = CoverageMeasure(
                covered=None,
                total=None,
                basis=CODE_UNAVAILABLE.format(reason="suite deadline reached before retrieval"),
            )
    report = build_report(ctx.suite, ctx.session_id, target_url, results, ctx.code, ctx.markers)
    return RunOutcome(report, aborted=False)
