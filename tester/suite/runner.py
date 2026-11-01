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
CODE_UNAVAILABLE = "Target code coverage unavailable: {reason}"


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
    components: set[str] = set()
    acceptance: set[str] = set()
    for test_id, evidence in (markers or {}).items():
        test = planned.get(test_id)
        if test is not None and test_id not in skipped:
            components.update(set(evidence["components"]) & set(test.components))
            acceptance.update(set(evidence["acceptance"]) & set(test.acceptance))
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
        if test.phase != "4":
            raise
        logger.error("Phase 4 check %s could not be evaluated: %s", test.check, type(exc).__name__)
        return result(
            Outcome.ERROR,
            f"The tester could not evaluate this case ({type(exc).__name__}).",
            ErrorSource.TESTER,
        )


def run_one(ctx: SuiteContext, test: PlannedTest) -> TestResult:
    if ctx.suite.manifest.phase != "4":
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
    report = build_report(ctx.suite, ctx.session_id, target_url, results, ctx.code, ctx.markers)
    return RunOutcome(report, aborted=False)
