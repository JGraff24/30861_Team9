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
    TestEndpointError,
    TesterSetupRequest,
    TestResult,
)
from tester.config import TargetAuth, TesterSettings
from tester.errors import TesterError, bad_request
from tester.store import ENDED, TesterSessionState, TesterStore
from tester.suite.checks import CHECKS, SuiteContext
from tester.suite.manifest import LoadedSuite, SuiteConfigurationError, load_suite
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
        return load_suite(self.settings.fixture_manifest_path, set(CHECKS))

    def setup(self, request: TesterSetupRequest, operator: str) -> SetupResponse:
        access = self._access(request.target_url)
        session_id = str(uuid.uuid4())
        self.store.create(session_id, operator, request.target_url, datetime.now(UTC))
        ready = False
        try:
            cleanup = (
                self.store.cleanup_candidates(request.target_url)
                if self.settings.tester_cleanup_prior_sessions
                else []
            )
            setup = {"cleanup_session_ids": cleanup} if cleanup else {}
            with self._client(
                request.target_url, access, self.settings.tester_setup_timeout_seconds
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
            logger.exception("Tester setup failed internally")
            raise TesterError(500, "Tester setup failed internally.", ErrorSource.TESTER) from None
        finally:
            # A failed setup is never runnable; it is queued for opt-in namespace cleanup.
            self.store.finish_setup(session_id, ready)

    def run(self, request: RunRequest, operator: str) -> bytes:
        session = self.store.require_owned(request.session_id, operator)
        if session.state != TesterSessionState.READY:
            raise TesterError(
                409, "Session is not ready; each session runs once.", ErrorSource.TESTER
            )
        try:
            suite = self.load_suite()
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

        def progress(results: list[TestResult]) -> None:
            results_so_far[:] = results
            self.store.save_progress(request.session_id, results)

        try:
            with self._client(
                session.target_url, access, self.settings.tester_run_timeout_seconds
            ) as client:
                client.session_id = request.session_id
                ctx = SuiteContext(
                    client, suite, request.session_id, sleep=self._sleep, clock=self._clock
                )
                outcome = run_suite(ctx, session.target_url, progress)
            report, aborted, message = outcome.report, outcome.aborted, outcome.abort_message
        except Exception:
            logger.exception("Tester aborted the suite")
            results = interrupted_results(
                suite, results_so_far, "The tester failed internally during this test."
            )
            code = CoverageMeasure(
                covered=None,
                total=None,
                basis=CODE_UNAVAILABLE.format(reason="tester aborted before retrieval"),
            )
            report = build_report(suite, request.session_id, session.target_url, results, code)
            aborted, message = True, "Tester aborted the suite after an internal failure."
        body = report.model_dump_json().encode("utf-8")
        # Store first: a lost response is recovered from GET /test/report.
        self.store.finish_run(request.session_id, body, aborted)
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
        running = self.store.running_sessions()
        if not running:
            return
        try:
            suite = self.load_suite()
        except SuiteConfigurationError:
            logger.error("Cannot rebuild interrupted reports: suite configuration unavailable")
            return
        code = CoverageMeasure(
            covered=None,
            total=None,
            basis=CODE_UNAVAILABLE.format(reason="tester process stopped during the suite"),
        )
        for session in running:
            results = interrupted_results(
                suite, session.results, "The tester process stopped during this test."
            )
            report = build_report(suite, session.session_id, session.target_url, results, code)
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
