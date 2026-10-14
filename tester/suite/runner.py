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
    SystemReport,
    TestResult,
)
from tester.suite.checks import (
    CHECKS,
    CheckFailed,
    Prerequisite,
    SuiteContext,
    SystemFault,
)
from tester.suite.manifest import LoadedSuite, PlannedTest
from tester.target import CommunicationError, DeadlineExceeded

logger = logging.getLogger(__name__)

COMPONENT_BASIS = (
    "Phase 3 component catalog C1-C6 in tests/fixtures/manifest.json; a component counts when "
    "a test declaring it executed its qualifying assertion (pass or fail)."
)
SYSTEM_BASIS = (
    "Phase 3 portions of AC-01/02/03/04/05/14/15/18 in tests/fixtures/manifest.json; an AC "
    "counts when a test declaring it executed its qualifying assertion (pass or fail)."
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
) -> SuiteReport:
    planned = {test.test_id: test for test in suite.manifest.tests}
    executed = [
        planned[result.test_id]
        for result in results
        if result.outcome in {Outcome.PASS, Outcome.FAIL} and result.test_id in planned
    ]
    components = {component for test in executed for component in test.components}
    acceptance = {criterion for test in executed for criterion in test.acceptance}
    coverage = SuiteCoverage(
        code=code,
        component=CoverageMeasure(
            covered=len(components), total=len(suite.manifest.components), basis=COMPONENT_BASIS
        ),
        system=CoverageMeasure(
            covered=len(acceptance), total=len(suite.manifest.acceptance), basis=SYSTEM_BASIS
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
    remaining = suite.manifest.tests[len(completed) :]
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


def run_one(ctx: SuiteContext, test: PlannedTest) -> TestResult:
    def result(outcome: Outcome, message: str, source: ErrorSource | None = None) -> TestResult:
        return TestResult(test_id=test.test_id, outcome=outcome, message=message, source=source)

    if test.phase != "3":
        return result(Outcome.SKIP, f"Planned for a later phase: {test.description}")
    try:
        return result(Outcome.PASS, CHECKS[test.check](ctx))
    except CheckFailed as exc:
        return result(Outcome.FAIL, str(exc))
    except Prerequisite as exc:
        return result(Outcome.SKIP, str(exc))
    except SystemFault as exc:
        return result(Outcome.ERROR, str(exc), ErrorSource.SYSTEM)
    except CommunicationError as exc:
        return result(Outcome.ERROR, f"No usable response: {exc}.", ErrorSource.COMMUNICATION)
    except DeadlineExceeded:
        raise
    except Exception as exc:  # A tester defect must not hide the remaining outcomes.
        logger.exception("Check %s failed inside the tester", test.test_id)
        return result(
            Outcome.ERROR,
            f"Tester check failed internally ({type(exc).__name__}).",
            ErrorSource.TESTER,
        )


def code_coverage(ctx: SuiteContext) -> CoverageMeasure:
    try:
        response = ctx.client.request(
            "GET", "/test/report", identity="test-admin", params={"session_id": ctx.session_id}
        )
    except (CommunicationError, DeadlineExceeded) as exc:
        return CoverageMeasure(
            covered=None, total=None, basis=CODE_UNAVAILABLE.format(reason=type(exc).__name__)
        )
    if response.status_code != 200:
        reason = f"system report returned HTTP {response.status_code}"
        return CoverageMeasure(
            covered=None, total=None, basis=CODE_UNAVAILABLE.format(reason=reason)
        )
    try:
        report = SystemReport.model_validate_json(response.content)
    except ValueError:
        reason = "system report did not match the agreed schema"
        return CoverageMeasure(
            covered=None, total=None, basis=CODE_UNAVAILABLE.format(reason=reason)
        )
    if report.session_id != ctx.session_id:
        reason = "system report named a different session"
        return CoverageMeasure(
            covered=None, total=None, basis=CODE_UNAVAILABLE.format(reason=reason)
        )
    return report.coverage.code


def run_suite(
    ctx: SuiteContext, target_url: str, progress: Callable[[list[TestResult]], None]
) -> RunOutcome:
    results: list[TestResult] = []
    for test in ctx.suite.manifest.tests:
        try:
            results.append(run_one(ctx, test))
        except DeadlineExceeded:
            message = "The tester's suite deadline was reached during this test."
            final = interrupted_results(ctx.suite, results, message)
            code = CoverageMeasure(
                covered=None,
                total=None,
                basis=CODE_UNAVAILABLE.format(reason="suite deadline reached before retrieval"),
            )
            report = build_report(ctx.suite, ctx.session_id, target_url, final, code)
            return RunOutcome(
                report, aborted=True, abort_message="Tester aborted: suite deadline reached."
            )
        progress(results)
    report = build_report(ctx.suite, ctx.session_id, target_url, results, code_coverage(ctx))
    return RunOutcome(report, aborted=False)
