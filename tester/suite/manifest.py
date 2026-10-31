"""Planned-suite manifest and independently authored fixture expectations.

Manifest major version 1 is the Phase 3 suite (C1-C6, eight Phase 3 AC portions, operations
1-14). Major version 2 is the Phase 4 suite of specification 2.1.0 (C1-C9, AC-01 to AC-11,
operations 1-14 plus the S, V and R paths). Both run through the same runner and checks.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, PositiveInt, ValidationError, model_validator

from app.contracts.models import (
    SHA256,
    AuditStatus,
    ContractModel,
    FindingCategory,
    FindingClassification,
    NonemptyText,
    SemVer,
)

ComponentId = Literal["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9"]
AcceptanceId = Literal[
    "AC-01",
    "AC-02",
    "AC-03",
    "AC-04",
    "AC-05",
    "AC-06",
    "AC-07",
    "AC-08",
    "AC-09",
    "AC-10",
    "AC-11",
    "AC-14",
    "AC-15",
    "AC-18",
]
PHASE3_COMPONENTS = ["C1", "C2", "C3", "C4", "C5", "C6"]
PHASE4_COMPONENTS = [*PHASE3_COMPONENTS, "C7", "C8", "C9"]
PHASE4_ACCEPTANCE = [f"AC-{number:02d}" for number in range(1, 12)]
ADDED_PATHS = ["S", "V", "R"]


class SuiteConfigurationError(Exception):
    """The manifest, expectations or fixture bytes cannot support an honest run."""


class FixturePending(Exception):
    """A planned fixture has no adopted bytes/expectations yet; dependent cases skip."""


OperationNumber = Annotated[int, Field(ge=1, le=14)]
# Phase 4 adds source (S), review context (V) and report (R) without renumbering 1-14.
OperationKey = OperationNumber | Literal["S", "V", "R"]


class PlannedTest(ContractModel):
    test_id: NonemptyText
    check: NonemptyText
    description: NonemptyText
    qualifying_assertion: NonemptyText
    components: list[ComponentId]
    acceptance: list[AcceptanceId]
    # Business operations this test exercises, and whether it checks their success or error path.
    operations: list[OperationKey] = Field(default_factory=list)
    path: Literal["success", "error"] | None = None
    phase: Literal["3", "4", "future"] = "3"
    prerequisites: list[NonemptyText] = Field(default_factory=list)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    # Phase 4: fixture packs this case needs (from the normative test plan).
    fixtures: list[NonemptyText] = Field(default_factory=list)


class Operation(ContractModel):
    number: OperationKey
    name: NonemptyText | None
    method: NonemptyText | None
    path: NonemptyText | None
    requirements: list[NonemptyText]
    phase3: bool
    phase4: bool = False
    note: NonemptyText


class SuiteLimits(ContractModel):
    """§10 shared external bounds the tester enforces for this suite."""

    suite_seconds: PositiveInt
    run_read_timeout_seconds: PositiveInt
    audit_poll_seconds: PositiveInt
    remediation_poll_seconds: PositiveInt
    business_get_seconds: PositiveInt
    upload_seconds: PositiveInt
    download_seconds: PositiveInt
    system_setup_seconds: PositiveInt
    json_response_bytes: PositiveInt
    binary_response_bytes: PositiveInt


class SuiteManifest(ContractModel):
    manifest_version: SemVer
    ruleset_version: SemVer
    specification_version: SemVer | None = None
    expectations: NonemptyText
    components: dict[ComponentId, NonemptyText]
    acceptance: dict[AcceptanceId, NonemptyText]
    operations: list[Operation]
    pending_confirmation: list[NonemptyText]
    known_deviations: list[NonemptyText] = Field(default_factory=list)
    fixture_hashes: dict[NonemptyText, SHA256] = Field(default_factory=dict)
    pack_revision: SemVer | None = None
    pack_hashes: dict[NonemptyText, SHA256] = Field(default_factory=dict)
    limits: SuiteLimits | None = None
    tests: list[PlannedTest]

    @property
    def phase(self) -> Literal["3", "4"]:
        return "4" if self.manifest_version.split(".")[0] == "2" else "3"

    @model_validator(mode="after")
    def complete_catalogs(self) -> Self:
        numbers = [operation.number for operation in self.operations]
        if self.phase == "3":
            if list(self.components) != PHASE3_COMPONENTS or len(self.acceptance) != 8:
                raise ValueError("Phase 3 catalogs are C1-C6 and the eight Phase 3 ACs")
            if numbers != list(range(1, 15)):
                raise ValueError("the operation catalog lists all 14 business operations in order")
        else:
            if list(self.components) != PHASE4_COMPONENTS:
                raise ValueError("Phase 4 component catalog is C1-C9 in order")
            if list(self.acceptance) != PHASE4_ACCEPTANCE:
                raise ValueError("Phase 4 system catalog is AC-01 to AC-11 in order")
            if numbers != [*range(1, 15), *ADDED_PATHS]:
                raise ValueError("Phase 4 operations are 1-14 then S, V, R")
            if self.limits is None or self.specification_version is None:
                raise ValueError("Phase 4 manifests declare limits and specification_version")
            phases = {test.phase for test in self.tests}
            if "3" in phases:
                raise ValueError("Phase 4 manifests plan phase 4 or future cases only")
        ids = [test.test_id for test in self.tests]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("planned test IDs must be present and unique")
        preceding: set[str] = set()
        for test in self.tests:
            if set(test.prerequisites) - preceding:
                raise ValueError(f"{test.test_id}: prerequisites must precede the test")
            preceding.add(test.test_id)
            if bool(test.operations) != (test.path is not None):
                raise ValueError(f"{test.test_id}: operations and path are declared together")
            if set(test.operations) - set(numbers):
                raise ValueError(f"{test.test_id}: operation outside the catalog")
            if set(test.components) - set(self.components):
                raise ValueError(f"{test.test_id}: component outside the catalog")
            if set(test.acceptance) - set(self.acceptance):
                raise ValueError(f"{test.test_id}: criterion outside the catalog")
        return self


class ExpectedFinding(ContractModel):
    ruleId: NonemptyText
    category: FindingCategory
    worksheet: str
    target: NonemptyText
    classification: FindingClassification


class FixtureReview(ContractModel):
    """§15.2 review record. Approval is never implied by a passing self-test."""

    status: Literal["pending", "approved", "changes_requested"]
    reviewers: list[NonemptyText] = Field(default_factory=list)
    date: NonemptyText | None = None
    notes: NonemptyText | None = None
    excel_open: Literal["not_checked", "no_repair_warning", "repair_warning"] = "not_checked"

    @model_validator(mode="after")
    def approval_needs_two_reviewers(self) -> Self:
        if self.status == "approved" and (len(set(self.reviewers)) < 2 or self.date is None):
            raise ValueError("approval needs two named reviewers and a date")
        return self


class FixtureBase(ContractModel):
    file: NonemptyText
    sha256: SHA256
    origin: Literal["synthetic", "team12", "team9"]
    upload_name: NonemptyText
    notes: NonemptyText
    review: FixtureReview | None = None


class AcceptedFixture(FixtureBase):
    kind: Literal["accepted"]
    expected_status: AuditStatus
    findings: list[ExpectedFinding]
    # Facts each limitation set must disclose (wording may differ between teams, §15.3).
    limitation_facts: list[NonemptyText] = Field(default_factory=list)
    min_limitations: int = Field(default=0, ge=0)
    # Phase 4 data for decisions, rule-vector cases and expected correction effects.
    extras: dict[str, JsonValue] = Field(default_factory=dict)


class RejectedFixture(FixtureBase):
    kind: Literal["rejected"]
    http_status: int
    error_code: NonemptyText | None = None


class PendingFixture(ContractModel):
    """A planned fixture key with no adopted bytes yet. Dependent cases skip with this reason."""

    kind: Literal["pending"]
    reason: NonemptyText


Fixture = Annotated[AcceptedFixture | RejectedFixture | PendingFixture, Field(discriminator="kind")]


class Expectations(ContractModel):
    ruleset_version: SemVer
    manual_review: NonemptyText
    fixtures: dict[NonemptyText, Fixture]


@dataclass(frozen=True)
class LoadedSuite:
    manifest: SuiteManifest
    expectations: Expectations
    data: dict[str, bytes]

    def fixture(self, name: str) -> AcceptedFixture | RejectedFixture:
        fixture = self.expectations.fixtures.get(name)
        if fixture is None:
            raise SuiteConfigurationError(f"fixture {name} is not declared")
        if isinstance(fixture, PendingFixture):
            raise FixturePending(f"Not executed: fixture {name} is not adopted ({fixture.reason})")
        return fixture

    def accepted(self, name: str) -> AcceptedFixture:
        fixture = self.fixture(name)
        if not isinstance(fixture, AcceptedFixture):
            raise SuiteConfigurationError(f"fixture {name} is not an accepted fixture")
        return fixture

    def rejected(self, name: str) -> RejectedFixture:
        fixture = self.fixture(name)
        if not isinstance(fixture, RejectedFixture):
            raise SuiteConfigurationError(f"fixture {name} is not a rejected fixture")
        return fixture

    def bytes_of(self, name: str) -> bytes:
        self.fixture(name)
        return self.data[name]


def referenced_fixtures(manifest: SuiteManifest) -> set[str]:
    names = {name for test in manifest.tests for name in test.fixtures}
    for test in manifest.tests:
        for key in ("fixture", "changed"):
            value = test.parameters.get(key)
            if isinstance(value, str):
                names.add(value)
    return names


def load_suite(manifest_path: Path, known_checks: set[str]) -> LoadedSuite:
    try:
        manifest = SuiteManifest.model_validate_json(manifest_path.read_bytes())
        expectations_path = manifest_path.parent / manifest.expectations
        expectations = Expectations.model_validate_json(expectations_path.read_bytes())
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise SuiteConfigurationError(f"cannot load suite manifest: {type(exc).__name__}") from exc
    if expectations.ruleset_version != manifest.ruleset_version:
        raise SuiteConfigurationError("manifest and expectations name different rulesets")
    if manifest.fixture_hashes and manifest.fixture_hashes != {
        name: fixture.sha256
        for name, fixture in expectations.fixtures.items()
        if not isinstance(fixture, PendingFixture)
    }:
        raise SuiteConfigurationError("manifest fixture hashes differ from the frozen answer key")
    unknown = {test.check for test in manifest.tests} - known_checks
    if unknown:
        raise SuiteConfigurationError(f"manifest names unknown checks: {sorted(unknown)}")
    required = {"clean", "seeded_issues", "corrupt"} | referenced_fixtures(manifest)
    for name in sorted(required):
        if name not in expectations.fixtures:
            raise SuiteConfigurationError(f"required fixture {name} is missing")
    return load_fixture_bytes(manifest, expectations, expectations_path.parent)


def load_fixture_bytes(
    manifest: SuiteManifest, expectations: Expectations, fixture_root: Path
) -> LoadedSuite:
    data = {}
    for name, fixture in expectations.fixtures.items():
        if isinstance(fixture, PendingFixture):
            continue
        path = (fixture_root / fixture.file).resolve()
        if fixture_root.resolve() not in path.parents:
            raise SuiteConfigurationError(f"fixture {name} is outside the fixture directory")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SuiteConfigurationError(f"fixture {name} is missing") from exc
        if hashlib.sha256(content).hexdigest() != fixture.sha256:
            raise SuiteConfigurationError(f"fixture {name} does not match its recorded SHA256")
        data[name] = content
    return LoadedSuite(manifest, expectations, data)


def snapshot_suite(suite: LoadedSuite, manifest_path: Path) -> str:
    saved = {
        "manifest": suite.manifest.model_dump(mode="json"),
        "expectations": suite.expectations.model_dump(mode="json"),
        "fixture_root": str((manifest_path.parent / suite.manifest.expectations).parent.resolve()),
    }
    if suite.manifest.phase == "4":
        # The accepted session owns its bytes even after fixtures are replaced by a
        # deployment. Keep legacy Phase3 snapshot semantics and retained plans intact.
        saved["fixture_bytes"] = {
            name: base64.b64encode(data).decode("ascii") for name, data in suite.data.items()
        }
    return json.dumps(saved)


def restore_suite(snapshot: str, *, read_fixtures: bool = True) -> LoadedSuite:
    try:
        saved = json.loads(snapshot)
        manifest = SuiteManifest.model_validate(saved["manifest"])
        expectations = Expectations.model_validate(saved["expectations"])
        if read_fixtures:
            if "fixture_bytes" in saved:
                encoded = saved["fixture_bytes"]
                expected = {
                    name: fixture
                    for name, fixture in expectations.fixtures.items()
                    if not isinstance(fixture, PendingFixture)
                }
                if not isinstance(encoded, dict) or set(encoded) != set(expected):
                    raise SuiteConfigurationError("stored fixture inventory differs from its plan")
                data = {}
                for name, fixture in expected.items():
                    content = base64.b64decode(encoded[name], validate=True)
                    if hashlib.sha256(content).hexdigest() != fixture.sha256:
                        raise SuiteConfigurationError(
                            "stored fixture bytes differ from recorded hash"
                        )
                    data[name] = content
                return LoadedSuite(manifest, expectations, data)
            # Existing prepared plans predate durable fixture snapshots. They retain
            # their original version and hash checks rather than inventing new bytes.
            return load_fixture_bytes(manifest, expectations, Path(saved["fixture_root"]))
        return LoadedSuite(manifest, expectations, {})
    except (ValueError, KeyError, TypeError) as exc:
        raise SuiteConfigurationError("stored suite plan is invalid") from exc
