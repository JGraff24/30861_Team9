"""Tester session lifecycle: setup, single-use run, immutable report retrieval."""

import logging
import socket
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from pydantic import TypeAdapter, ValidationError

from app.contracts.models import (
    CoverageMeasure,
    ErrorSource,
    Identifier,
    RunRequest,
    SetupResponse,
    SuiteCoverage,
    SuiteReport,
    TestEndpointError,
    TesterSetupRequest,
    TestResult,
)
from tester.config import TargetAuth, TesterSettings
from tester.errors import TesterError, bad_request
from tester.store import ENDED, TesterSessionState, TesterStore
from tester.suite.checks import SuiteContext
from tester.suite.manifest import (
    LoadedSuite,
    SuiteConfigurationError,
    load_suite,
    restore_suite,
    snapshot_suite,
)
from tester.suite.registry import names as check_names
from tester.suite.runner import (
    CODE_UNAVAILABLE,
    build_report,
    interrupted_results,
    run_suite,
)
from tester.target import CommunicationError, DeadlineExceeded, Resolver, TargetClient

logger = logging.getLogger(__name__)
IDENTIFIER = TypeAdapter(Identifier)
SYSTEM_SETUP_TIMEOUT_SECONDS = 30


class TesterService:
    def __init__(
        self,
        settings: TesterSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.store = TesterStore(settings.tester_db_path)
        self._transport = transport
        self._resolver = resolver
        self._sleep = sleep
        self._clock = clock

    def _access(self, target_url: str) -> TargetAuth:
        access = self.settings.target_auth_map.get(target_url)
        if target_url not in self.settings.allowed_target_urls or access is None:
            raise bad_request("target_url is not an approved, configured system base URL.")
        return access

    def _client(self, target_url: str, access: TargetAuth, seconds: float) -> TargetClient:
        return TargetClient(
            target_url,
            access,
            self.settings.app_env,
            self._clock() + seconds,
            transport=self._transport,
            resolver=self._resolver,
            clock=self._clock,
        )

    def load_suite(self) -> LoadedSuite:
        return load_suite(self.settings.fixture_manifest_path, check_names())

    def setup(self, request: TesterSetupRequest, operator: str) -> SetupResponse:
        access = self._access(request.target_url)
        deadline = self._clock() + self.settings.tester_setup_timeout_seconds
        try:
            suite = self.load_suite()
        except SuiteConfigurationError:
            raise TesterError(
                500, "Tester suite configuration is unavailable.", ErrorSource.TESTER
            ) from None
        session_id = str(uuid.uuid4())
        # Failed preparations always receive deferred compensation. Optional cleanup of
        # successfully retrieved namespaces remains an explicit operator setting.
        cleanup = self.store.cleanup_candidates(
            request.target_url, failed_only=not self.settings.tester_cleanup_prior_sessions
        )
        setup: dict[str, object] = {"cleanup_session_ids": cleanup} if cleanup else {}
        self.store.create(
            session_id,
            operator,
            request.target_url,
            datetime.now(UTC),
            plan=snapshot_suite(suite, self.settings.fixture_manifest_path),
            setup=setup,
        )
        ready = False
        try:
            with self._client(
                request.target_url, access, max(0, deadline - self._clock())
            ) as client:
                try:
                    response = client.request(
                        "POST",
                        "/test/setup",
                        identity="test-admin",
                        json={"session_id": session_id, "setup": setup},
                        timeout=SYSTEM_SETUP_TIMEOUT_SECONDS,
                    )
                except (CommunicationError, DeadlineExceeded):
                    raise TesterError(
                        500,
                        "System setup failed: no response from the target.",
                        ErrorSource.COMMUNICATION,
                    ) from None
            if response.status_code != 201:
                raise forwarded_setup_error(response)
            try:
                echoed = SetupResponse.model_validate_json(response.content)
            except ValueError:
                echoed = None
            if echoed is None or echoed.session_id != session_id:
                raise TesterError(
                    500, "System setup did not return the same session_id.", ErrorSource.SYSTEM
                )
            if cleanup:
                self.store.clear_cleanup(cleanup)
            ready = True
            return SetupResponse(session_id=session_id)
        except TesterError:
            raise
        except Exception:
            logger.error("Tester setup failed internally")
            raise TesterError(500, "Tester setup failed internally.", ErrorSource.TESTER) from None
        finally:
            # A failed setup is never runnable; compensate through the next new setup.
            self.store.finish_setup(session_id, ready)

    def run(self, request: RunRequest, operator: str) -> bytes:
        session = self.store.require_owned(request.session_id, operator)
        if session.state != TesterSessionState.READY:
            raise TesterError(
                409, "Session is not ready; each session runs once.", ErrorSource.TESTER
            )
        try:
            if session.plan is None:
                raise SuiteConfigurationError(
                    "legacy session has no stored plan; create a new session"
                )
            suite = restore_suite(session.plan)
            if {test.check for test in suite.manifest.tests} - check_names():
                raise SuiteConfigurationError("stored plan uses checks unavailable in this release")
            access = self._access(session.target_url)
        except (SuiteConfigurationError, TesterError) as exc:
            logger.error("Run refused before consuming session: %s", exc)
            raise TesterError(
                500,
                "Tester suite configuration is unavailable; session not consumed.",
                ErrorSource.TESTER,
            ) from None
        self.store.begin_run(request.session_id, operator)
        results_so_far: list[TestResult] = []
        markers_so_far: dict[str, dict[str, list[str]]] = {}

        def progress(results: list[TestResult], markers: dict[str, dict[str, list[str]]]) -> None:
            results_so_far[:] = results
            markers_so_far.update(markers)
            self.store.save_progress(request.session_id, results, markers)

        ctx: SuiteContext | None = None
        try:
            # A Phase 4 plan carries the section 10 suite budget; Phase 3 keeps the setting.
            seconds = (
                suite.manifest.limits.suite_seconds
                if suite.manifest.limits is not None
                else self.settings.tester_run_timeout_seconds
            )
            with self._client(session.target_url, access, seconds) as client:
                client.session_id = request.session_id
                ctx = SuiteContext(
                    client, suite, request.session_id, sleep=self._sleep, clock=self._clock
                )
                outcome = run_suite(ctx, session.target_url, progress)
            report, aborted, message = outcome.report, outcome.aborted, outcome.abort_message
        except Exception:
            logger.error("Tester aborted the suite")
            if ctx is not None:
                markers_so_far.update(ctx.markers)
            results = interrupted_results(
                suite, results_so_far, "The tester failed internally during this test."
            )
            code = CoverageMeasure(
                covered=None,
                total=None,
                basis=CODE_UNAVAILABLE.format(reason="tester aborted before retrieval"),
            )
            report = build_report(
                suite, request.session_id, session.target_url, results, code, markers_so_far
            )
            aborted, message = True, "Tester aborted the suite after an internal failure."
        body = report.model_dump_json().encode("utf-8")
        # Store first: a lost response is recovered from GET /test/report.
        self.store.finish_run(request.session_id, body, aborted)
        auxiliary = list(dict.fromkeys(ctx.state.get("aux_sessions", []))) if ctx else []
        if auxiliary:
            # Evidence namespaces are retained until the run is recorded; the next setup for
            # this target cleans them through cleanup_session_ids (section 14.1).
            self.store.record_auxiliary(
                request.session_id,
                session.target_url,
                auxiliary,
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        if aborted:
            raise TesterError(500, message or "Tester aborted the suite.", ErrorSource.TESTER)
        return body

    def report(self, session_id: str | None, operator: str) -> bytes:
        try:
            valid_id = IDENTIFIER.validate_python(session_id)
        except ValidationError:
            raise bad_request("session_id query parameter is required and must be valid.") from None
        session = self.store.require_owned(valid_id, operator)
        if session.state not in ENDED or session.report is None:
            raise TesterError(409, "The suite for this session has not ended.", ErrorSource.TESTER)
        self.store.mark_retrieved(valid_id)
        return session.report

    def recover_interrupted_runs(self) -> None:
        """Close runs a previous tester process left RUNNING, keeping completed outcomes."""
        if not self.store.exists():
            return
        self.store.recover_preparing()
        running = self.store.running_sessions()
        if not running:
            return
        code = CoverageMeasure(
            covered=None,
            total=None,
            basis=CODE_UNAVAILABLE.format(reason="tester process stopped during the suite"),
        )
        for session in running:
            restored = None
            if session.plan is not None:
                try:
                    restored = restore_suite(session.plan, read_fixtures=False)
                except SuiteConfigurationError:
                    restored = None
            if restored is None:
                # Pre-snapshot releases cannot reconstruct unknown unexecuted tests.
                # Retain their recorded outcomes and disclose that limitation explicitly.
                unknown = CoverageMeasure(
                    covered=None,
                    total=None,
                    basis=(
                        "Legacy session has no saved plan/markers; "
                        "completeness cannot be reconstructed."
                    ),
                )
                report = SuiteReport(
                    session_id=session.session_id,
                    target_url=session.target_url,
                    results=session.results,
                    coverage=SuiteCoverage(code=code, component=unknown, system=unknown),
                )
            else:
                suite = restored
                results = interrupted_results(
                    suite, session.results, "The tester process stopped during this test."
                )
                report = build_report(
                    suite, session.session_id, session.target_url, results, code, session.markers
                )
            self.store.finish_run(
                session.session_id, report.model_dump_json().encode("utf-8"), aborted=True
            )
            logger.warning("Recorded interrupted tester session as aborted")


def forwarded_setup_error(response: httpx.Response) -> TesterError:
    """Preserve the system's status, message and source when it sent a valid error body."""
    try:
        error = TestEndpointError.model_validate_json(response.content)
    except ValueError:
        error = None
    if response.status_code >= 400 and error is not None:
        return TesterError(response.status_code, error.message, error.source)
    if response.status_code >= 400:
        return TesterError(
            response.status_code if response.status_code >= 500 else 500,
            f"System setup failed with HTTP {response.status_code}.",
            ErrorSource.SYSTEM,
        )
    return TesterError(
        500, f"System setup returned unexpected HTTP {response.status_code}.", ErrorSource.SYSTEM
    )
