"""DTO catalogs describe pending wire shapes, never pretend delivered endpoints."""

import hashlib
import json

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from app.contracts.models import CoverageMeasure, TestResult
from scripts.check_identity import check_vectors
from scripts.export_schemas import CONTRACT_DIR, schema_documents


def test_openapi_catalogs_are_valid_current_and_reflect_delivered_routes() -> None:
    for name, expected in schema_documents().items():
        stored = json.loads((CONTRACT_DIR / f"{name}.openapi.json").read_text(encoding="utf-8"))
        assert stored == expected
        validate(stored)
        assert len(stored["paths"]) == (15 if name == "system" else 3)
        if name == "tester":
            assert set(stored["paths"]) == {"/test/setup", "/test/run", "/test/report"}
        if name == "system":
            assert stored["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
            assert stored["paths"]["/api/v1/workbooks"]["post"]["security"] == [{"BearerAuth": []}]
        assert stored["openapi"] == "3.1.1"
        assert stored["x-instructor-interface-version"] == "0.4.2"
        for schema in stored["components"]["schemas"].values():
            Draft202012Validator.check_schema(schema)


def test_independent_valid_version_payload_and_corrupted_hash() -> None:
    document = json.loads((CONTRACT_DIR / "system.openapi.json").read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["VersionResponse"]
    validator = Draft202012Validator(
        {
            **schema,
            "components": document["components"],
        },
        format_checker=FormatChecker(),
    )
    payload = {
        "workbookId": "w",
        "versionId": "v",
        "fileName": "book.xlsx",
        "sha256": "0" * 64,
        "createdAt": "2026-10-14T16:00:00Z",
        "status": "PROCESSING",
        "latestAuditId": None,
        "sourceVersionId": None,
    }
    validator.validate(payload)
    assert list(validator.iter_errors(payload | {"sha256": "not-a-hash"}))
    assert list(validator.iter_errors(payload | {"createdAt": "not-a-date"}))
    assert list(validator.iter_errors(payload | {"workbook_id": "w"}))
    assert list(
        validator.iter_errors(
            {key: value for key, value in payload.items() if key != "sourceVersionId"}
        )
    )


def test_coverage_schema_rejects_unpaired_null_and_negative_counts() -> None:
    validator = Draft202012Validator(CoverageMeasure.model_json_schema(mode="serialization"))
    validator.validate({"covered": None, "total": None, "basis": "Unavailable"})
    validator.validate({"covered": 0, "total": 0, "basis": "Empty scope"})
    for payload in (
        {"covered": None, "total": 6, "basis": "Unavailable"},
        {"covered": -1, "total": 6, "basis": "Lines"},
        {"covered": 0, "total": 6, "basis": ""},
        {"covered": 0, "total": 6, "basis": " "},
    ):
        assert list(validator.iter_errors(payload))


def test_report_schema_requires_error_source_and_omits_it_elsewhere() -> None:
    schema = TestResult.model_json_schema(mode="serialization")
    validator = Draft202012Validator(schema)
    validator.validate({"test_id": "T-01", "outcome": "pass", "message": "Matched"})
    validator.validate(
        {"test_id": "T-02", "outcome": "error", "message": "Timeout", "source": "communication"}
    )
    for payload in (
        {"test_id": "T-01", "outcome": "pass", "message": "Matched", "source": None},
        {"test_id": "T-02", "outcome": "error", "message": "Timeout"},
        {"test_id": "T-02", "outcome": "error", "message": "Timeout", "source": None},
    ):
        assert list(validator.iter_errors(payload))


def test_independent_identity_vectors_match_packet_and_preserve_unicode() -> None:
    digests = check_vectors()
    assert digests[0] == "502d6624551c9da5c9a80d7efcc04be1119dad4a16f55171487949600a177714"
    assert digests[1] != digests[2]


def test_packet_profile_is_preserved_verbatim() -> None:
    profile = (CONTRACT_DIR / "phase3.md").read_text(encoding="utf-8")
    assert "# Shared Phase 3 specification" in profile
    assert "## Contract completion appendix for bootstrap and all owners" in profile
    assert "Team 9 implementation defaults" in profile
    shared = profile[profile.index("# Shared Phase 3 specification") :]
    assert hashlib.sha256(shared.encode("utf-8")).hexdigest() == (
        "f999185e49e1d109129e5b65db173e48fbc3dec1e4b8e62300ef015c56545468"
    )
