"""Planned-suite manifest and independently authored fixture expectations."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from app.contracts.models import (
    SHA256,
    AuditStatus,
    ContractModel,
    FindingCategory,
    FindingClassification,
    NonemptyText,
    SemVer,
)

ComponentId = Literal["C1", "C2", "C3", "C4", "C5", "C6"]
AcceptanceId = Literal["AC-01", "AC-02", "AC-03", "AC-04", "AC-05", "AC-14", "AC-15", "AC-18"]


class SuiteConfigurationError(Exception):
    """The manifest, expectations or fixture bytes cannot support an honest run."""


class PlannedTest(ContractModel):
    test_id: NonemptyText
    check: NonemptyText
    description: NonemptyText
    qualifying_assertion: NonemptyText
    components: list[ComponentId]
    acceptance: list[AcceptanceId]
    phase: Literal["3", "future"] = "3"


class SuiteManifest(ContractModel):
    manifest_version: SemVer
    ruleset_version: SemVer
    expectations: NonemptyText
    components: dict[ComponentId, NonemptyText]
    acceptance: dict[AcceptanceId, NonemptyText]
    tests: list[PlannedTest]

    @model_validator(mode="after")
    def complete_catalogs(self) -> Self:
        if len(self.components) != 6 or len(self.acceptance) != 8:
            raise ValueError("Phase 3 catalogs are C1-C6 and the eight Phase 3 ACs")
        ids = [test.test_id for test in self.tests]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("planned test IDs must be present and unique")
        return self


class ExpectedFinding(ContractModel):
    ruleId: NonemptyText
    category: FindingCategory
    worksheet: str
    target: NonemptyText
    classification: FindingClassification


class FixtureBase(ContractModel):
    file: NonemptyText
    sha256: SHA256
    origin: Literal["synthetic", "team12"]
    upload_name: NonemptyText
    notes: NonemptyText


class AcceptedFixture(FixtureBase):
    kind: Literal["accepted"]
    expected_status: AuditStatus
    findings: list[ExpectedFinding]


class RejectedFixture(FixtureBase):
    kind: Literal["rejected"]
    http_status: int
    error_code: NonemptyText | None = None


Fixture = Annotated[AcceptedFixture | RejectedFixture, Field(discriminator="kind")]


class Expectations(ContractModel):
    ruleset_version: SemVer
    manual_review: NonemptyText
    fixtures: dict[NonemptyText, Fixture]


@dataclass(frozen=True)
class LoadedSuite:
    manifest: SuiteManifest
    expectations: Expectations
    data: dict[str, bytes]

    def accepted(self, name: str) -> AcceptedFixture:
        fixture = self.expectations.fixtures[name]
        if not isinstance(fixture, AcceptedFixture):
            raise SuiteConfigurationError(f"fixture {name} is not an accepted fixture")
        return fixture

    def rejected(self, name: str) -> RejectedFixture:
        fixture = self.expectations.fixtures[name]
        if not isinstance(fixture, RejectedFixture):
            raise SuiteConfigurationError(f"fixture {name} is not a rejected fixture")
        return fixture


def load_suite(manifest_path: Path, known_checks: set[str]) -> LoadedSuite:
    try:
        manifest = SuiteManifest.model_validate_json(manifest_path.read_bytes())
        expectations_path = manifest_path.parent / manifest.expectations
        expectations = Expectations.model_validate_json(expectations_path.read_bytes())
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise SuiteConfigurationError(f"cannot load suite manifest: {type(exc).__name__}") from exc
    if expectations.ruleset_version != manifest.ruleset_version:
        raise SuiteConfigurationError("manifest and expectations name different rulesets")
    unknown = {test.check for test in manifest.tests} - known_checks
    if unknown:
        raise SuiteConfigurationError(f"manifest names unknown checks: {sorted(unknown)}")
    data = {}
    for name, fixture in expectations.fixtures.items():
        path = (expectations_path.parent / fixture.file).resolve()
        if expectations_path.parent.resolve() not in path.parents:
            raise SuiteConfigurationError(f"fixture {name} is outside the fixture directory")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SuiteConfigurationError(f"fixture {name} is missing") from exc
        if hashlib.sha256(content).hexdigest() != fixture.sha256:
            raise SuiteConfigurationError(f"fixture {name} does not match its recorded SHA256")
        data[name] = content
    return LoadedSuite(manifest, expectations, data)
