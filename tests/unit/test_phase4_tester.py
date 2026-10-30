"""Phase 4 tester: frozen contract, policy oracle, manifest binding, comparator and full runs.

The full runs use the test-only fake target in tests/support/fake_phase4.py. Mutated fakes
prove that the independent checks fail on real defects instead of passing vacuously.
"""

import hashlib
import io
import json
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts.build_phase4_manifest import build, render
from scripts.check_phase4_policy import EXAMPLES, VECTORS, check_examples, check_policy
from tester.config import TesterSettings as SettingsForTester
from tester.main import TesterServices as ServicesForTester
from tester.main import create_app
from tester.phase4 import contract, policy
from tester.phase4.package import compare_packages, read_package
from tester.phase4.workbooks import phase4_fixtures
from tester.suite import checks
from tester.suite.manifest import (
    AcceptedFixture,
    Expectations,
    PendingFixture,
    SuiteManifest,
    load_suite,
)
from tester.suite.records import run_record
from tester.suite.registry import names as check_names
from tests.support.fake_phase4 import BASE_URL, FakePhase4, access_map
from tests.support.fake_system import address_resolver

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
MANIFEST = FIXTURES / "manifest-phase4.json"
EXPECTATIONS = FIXTURES / "expectations-phase4.json"
PLAN = REPO / "docs" / "contracts" / "phase4" / "phase4-test-plan.json"
OPERATOR = "phase4-operator-token"
SMALL = {"upload_limit": 60_000, "worksheet_limit": 5, "cell_limit": 100}


@pytest.fixture(autouse=True)
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, item in SettingsForTester.model_fields.items():
        monkeypatch.delenv(str(item.validation_alias or name.upper()), raising=False)
    monkeypatch.setattr(checks, "UPLOAD_LIMIT_BYTES", SMALL["upload_limit"])
    monkeypatch.setattr(checks, "WORKSHEET_LIMIT", SMALL["worksheet_limit"])
    monkeypatch.setattr(checks, "NONEMPTY_CELL_LIMIT", SMALL["cell_limit"])


# --- frozen contract ---------------------------------------------------------------------


def test_examples_and_policy_vectors_match_the_frozen_pack() -> None:
    assert len(contract.definitions()) == 44
    lines = check_policy(json.loads(VECTORS.read_text(encoding="utf-8")))
    lines += check_examples(json.loads(EXAMPLES.read_text(encoding="utf-8")))
    assert len(lines) == 10


def test_packaged_schema_copy_is_the_adopted_pack() -> None:
    pack = REPO / "docs" / "contracts" / "phase4" / contract.SCHEMA_FILE
    assert json.loads(contract.schema_path().read_text("utf-8")) == json.loads(
        pack.read_text("utf-8")
    )
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"docs/contracts/phase4/phase4-wire-schemas.json" = "tester/phase4/' in pyproject


@pytest.mark.parametrize(
    ("definition", "body"),
    [
        (
            "HealthResponse",
            {"status": "AVAILABLE", "apiVersion": "1.0", "rulesetVersion": "2.0.0", "x": 1},
        ),
        ("HealthResponse", {"status": "AVAILABLE", "apiVersion": "1.0"}),
        ("TestEndpointError", {"message": "exists", "source": "system", "code": "DUP"}),
        ("TestEndpointError", {"message": " ", "source": "system"}),
        ("ProjectError", {"code": "X", "message": "m", "requestId": "bad id", "details": {}}),
        ("UploadResponse", {"workbookId": "w", "versionId": "v", "auditId": "a", "status": "DONE"}),
        ("ReauditRequest", {"reason": "SCHEDULED"}),
        ("HeaderRowValue", {"headerRow": True}),
        ("Decision", {"findingId": "0" * 64, "action": "DECLINE", "value": {"text": "x"}}),
        ("Decision", {"findingId": "0" * 64, "action": "APPLY"}),
        ("FaultConfiguration", {"audit_failures": 2}),
        ("FaultConfiguration", {"audit_failures": True}),
    ],
)
def test_strict_wire_shapes_reject_extra_missing_or_coerced_fields(
    definition: str, body: Any
) -> None:
    assert contract.violations(definition, body)


def test_timestamps_must_be_utc_rfc3339_with_z() -> None:
    version = {
        "workbookId": "w",
        "versionId": "v",
        "fileName": "a.xlsx",
        "sha256": "0" * 64,
        "createdAt": "2026-10-29T16:00:00Z",
        "status": "PROCESSING",
        "latestAuditId": None,
        "sourceVersionId": None,
    }
    contract.validate("VersionResponse", version)
    for bad in ("2026-10-29T16:00:00+00:00", "2026-10-29 16:00:00Z", "not-a-dateZ"):
        assert contract.violations("VersionResponse", version | {"createdAt": bad})


def test_json_parsing_rejects_duplicate_keys_and_constants() -> None:
    with pytest.raises(contract.ContractViolation):
        contract.parse_json(b'{"session_id":"a","session_id":"b"}', "SetupResponse")
    with pytest.raises(contract.ContractViolation):
        contract.parse_json(b'{"covered":NaN,"total":1,"basis":"x"}', "CoverageMeasure")
    with pytest.raises(contract.ContractViolation):
        contract.parse_json(b"\xff", "SetupResponse")


# --- policy oracle -------------------------------------------------------------------------


def test_policy_text_rules() -> None:
    assert policy.trim("  Sheet3 　") == "Sheet3"
    assert policy.trim("​Sheet1") == "​Sheet1"
    assert policy.collapse(" click   HERE ") == "click HERE"
    assert policy.is_generic_sheet_name("SHEET12")
    assert not policy.is_generic_sheet_name("Sheet00")
    assert policy.is_ambiguous_label("Read more!")
    assert not policy.is_ambiguous_label("Read more!!")
    assert policy.is_ambiguous_label("HTTPS://example.edu/report")
    assert policy.is_ambiguous_label("  ")
    assert not policy.is_ambiguous_label("=Overview of services")


def test_policy_contrast_and_tint_boundaries() -> None:
    assert policy.contrast_passes(4.5, large=False)
    assert not policy.contrast_passes(4.4999999, large=False)
    assert policy.contrast_passes(3.0, large=True)
    assert policy.is_large_text(18, False) and policy.is_large_text(14, True)
    assert not policy.is_large_text(14, False)
    assert policy.parse_rgb("FF1F4E78") == (0x1F, 0x4E, 0x78)
    assert policy.hex_rgb(policy.apply_tint((0x33, 0x66, 0x99), 0.5)) == "8CB2D9"
    with pytest.raises(ValueError):
        policy.parse_rgb("12345")
    with pytest.raises(ValueError):
        policy.apply_tint((0, 0, 0), 1.5)


def test_policy_components_use_sparse_sets_and_four_neighbors() -> None:
    cells = {(1, 1): "Name", (1, 2): "Score", (2, 1): "Ada", (2, 2): 9, (1_000_000, 16_000): "far"}
    found = policy.components(cells)
    assert [c.range for c in found] == ["A1:B2", "WQJ1000000:WQJ1000000"]
    assert policy.candidate_classification(found[0], cells) == "REQUIRES_USER_INPUT"
    assert policy.candidate_classification(found[1], cells) is None
    inside = policy.components(cells, tables=[(1, 1, 2, 2)])
    assert [c.range for c in inside] == ["WQJ1000000:WQJ1000000"]
    duplicated = {(1, 1): "Name", (1, 2): " name ", (2, 1): 1, (2, 2): 2}
    assert (
        policy.candidate_classification(policy.components(duplicated)[0], duplicated)
        == "MANUAL_REVIEW"
    )
    with pytest.raises(ValueError):
        policy.parse_cell("$A$1")


# --- manifest and expectations ---------------------------------------------------------


def test_committed_manifest_is_generated_from_the_normative_plan() -> None:
    assert MANIFEST.read_text(encoding="utf-8") == render(build())
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    hosted = [entry for entry in plan["tests"] if entry["tier"] == "hosted_http"]
    manifest = SuiteManifest.model_validate_json(MANIFEST.read_bytes())
    assert manifest.phase == "4"
    assert [test.test_id for test in manifest.tests] == [entry["test_id"] for entry in hosted]
    for test, entry in zip(manifest.tests, hosted, strict=True):
        assert test.components == entry["components"]
        assert test.acceptance == entry["acceptance"]
        assert test.operations == entry["operations"]
        assert test.prerequisites == entry["prerequisites"]
        assert test.check in check_names()
    assert [o.number for o in manifest.operations][-3:] == ["S", "V", "R"]
    assert manifest.limits is not None and manifest.limits.suite_seconds == 1800


def test_phase4_suite_loads_with_every_referenced_fixture_declared() -> None:
    suite = load_suite(MANIFEST, check_names())
    pending = {k for k, v in suite.expectations.fixtures.items() if isinstance(v, PendingFixture)}
    assert pending == set()
    assert suite.manifest.specification_version == "2.1.0"


def test_expectations_are_internally_consistent() -> None:
    expectations = Expectations.model_validate_json(EXPECTATIONS.read_bytes())
    for name, fixture in expectations.fixtures.items():
        if not isinstance(fixture, AcceptedFixture):
            continue
        keys = [(f.ruleId, f.worksheet, f.target) for f in fixture.findings]
        assert keys == sorted(keys), name
        assert len(set(keys)) == len(keys), name
        for finding in fixture.findings:
            assert str(finding.category) == policy.CATEGORY[finding.ruleId], name
        rows = [
            ("", f.ruleId, "", f.worksheet, f.target, str(f.classification))
            for f in fixture.findings
        ]
        from tester.phase4.checks import status_for

        assert status_for(rows) == fixture.expected_status, name
        assert fixture.review is not None and fixture.review.status in ("pending", "approved"), name
        targets = {(f.ruleId, f.target) for f in fixture.findings}
        decisions = fixture.extras.get("decisions", {})
        assert isinstance(decisions, dict)
        for decision in decisions.values():
            assert isinstance(decision, dict)
            assert tuple(decision["finding"]) in targets, name
            assert ("value" in decision) == (decision["action"] == "APPLY"), name
            if "value" in decision:
                rule = decision["finding"][0]
                definition = {
                    "ALT_TEXT": "AltTextValue",
                    "WORKSHEET_NAME": "WorksheetNameValue",
                    "LINK_TEXT": "LinkTextValue",
                    "TABLE_HEADER": "HeaderRowValue",
                }[rule]
                contract.validate(definition, decision["value"])
        plans = fixture.extras.get("plans", {})
        assert isinstance(plans, dict)
        for plan in plans.values():
            assert isinstance(plan, dict)
            assert set(plan["decisions"]) <= set(decisions), name
        for entry in fixture.extras.get("review", []) or []:
            assert isinstance(entry, dict)
            assert tuple(entry["finding"]) in targets, name


def test_phase4_fixture_bytes_are_deterministic_and_committed() -> None:
    expectations = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))["fixtures"]
    for file_name, data in phase4_fixtures().items():
        assert phase4_fixtures()[file_name] == data
        key = file_name.removesuffix(".xlsx")
        assert expectations[key]["sha256"] == hashlib.sha256(data).hexdigest(), key
        entries = read_package(data)
        assert "[Content_Types].xml" in entries and "xl/workbook.xml" in entries
    assert len(phase4_fixtures()["large_preservation.xlsx"]) > 2_097_152


def test_phase4_fixtures_open_in_openpyxl() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    for name, data in phase4_fixtures().items():
        workbook = openpyxl.load_workbook(io.BytesIO(data))
        assert workbook.sheetnames, name


# --- package comparator ------------------------------------------------------------------


def _effects(fixture: str, plan: str) -> list[dict[str, Any]]:
    expectations = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))["fixtures"]
    effects: list[dict[str, Any]] = expectations[fixture]["extras"]["plans"][plan]["effects"]
    return effects


def _decisions(fixture: str, plan: str) -> list[dict[str, Any]]:
    expectations = json.loads(EXPECTATIONS.read_text(encoding="utf-8"))["fixtures"][fixture]
    table = expectations["extras"]["decisions"]
    rows = []
    for name in expectations["extras"]["plans"][plan]["decisions"]:
        item = table[name]
        worksheet = next(
            f["worksheet"]
            for f in expectations["findings"]
            if [f["ruleId"], f["target"]] == item["finding"]
        )
        rows.append(
            {
                "ruleId": item["finding"][0],
                "target": item["finding"][1],
                "worksheet": worksheet,
                "action": item["action"],
                "value": item.get("value"),
            }
        )
    return rows


PLANS = [
    ("mixed_decisions", "mixed"),
    ("mixed_decisions", "combined"),
    ("alt_supported", "alt"),
    ("link_supported", "link"),
    ("link_supported", "string_label"),
    ("header_range", "header_range"),
    ("header_disabled_table", "header_table"),
    ("rename_references", "rename"),
    ("preservation_rich", "preserve"),
]


@pytest.mark.parametrize(("fixture", "plan"), PLANS)
def test_comparator_accepts_exactly_the_approved_edits(fixture: str, plan: str) -> None:
    source = phase4_fixtures()[f"{fixture}.xlsx"]
    output = FakePhase4().edit(source, _decisions(fixture, plan))
    assert compare_packages(source, output, _effects(fixture, plan)) == []


def _rewrite(data: bytes, change: Callable[[dict[str, bytes]], None]) -> bytes:
    archive = zipfile.ZipFile(io.BytesIO(data))
    parts = {name: archive.read(name) for name in archive.namelist()}
    change(parts)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as out:
        for name, content in parts.items():
            out.writestr(name, content)
    return stream.getvalue()


def _sub(part: str, old: str, new: str) -> Callable[[dict[str, bytes]], None]:
    def change(parts: dict[str, bytes]) -> None:
        text = parts[part].decode()
        assert old in text
        parts[part] = text.replace(old, new, 1).encode()

    return change


@pytest.mark.parametrize(
    ("fixture", "plan", "change", "expected"),
    [
        (
            "preservation_rich",
            "preserve",
            _sub("docProps/app.xml", "Microsoft Excel", "Other"),
            "unexplained change in docProps/app.xml",
        ),
        (
            "preservation_rich",
            "preserve",
            _sub("xl/drawings/drawing1.xml", 'name="Picture 1"', 'name="Renamed"'),
            "unexplained semantic change in xl/drawings/drawing1.xml",
        ),
        (
            "preservation_rich",
            "preserve",
            _sub("xl/drawings/drawing1.xml", "A small white", "A large white"),
            "title/description differ",
        ),
        (
            "preservation_rich",
            "preserve",
            lambda parts: parts.pop("customXml/team9-opaque.bin"),
            "missing",
        ),
        (
            "preservation_rich",
            "preserve",
            lambda parts: parts.__setitem__("xl/extra.xml", b"<x/>"),
            "unexplained entry xl/extra.xml",
        ),
        (
            "link_supported",
            "link",
            _sub("xl/sharedStrings.xml", "<t>here</t>", "<t>Accessibility policy (PDF)</t>"),
            "unexplained change in xl/sharedStrings.xml",
        ),
        (
            "link_supported",
            "string_label",
            _sub(
                "xl/worksheets/sheet1.xml",
                't="inlineStr"><is><t xml:space="preserve">=Overview of services</t></is></c>',
                "><f>Overview of services</f></c>",
            ),
            "inline-string",
        ),
        (
            "rename_references",
            "rename",
            _sub("xl/worksheets/sheet2.xml", 'LEN("Sheet1")', "LEN(\"'Data source'\")"),
            "unexplained semantic change in xl/worksheets/sheet2.xml",
        ),
        (
            "rename_references",
            "rename",
            _sub("xl/charts/chart1.xml", "'Data source'!$B$2:$B$3", "Sheet1!$B$2:$B$3"),
            "xl/charts/chart1.xml",
        ),
        (
            "header_range",
            "header_range",
            _sub("[Content_Types].xml", "spreadsheetml.table+xml", "spreadsheetml.tablex+xml"),
            "content type override",
        ),
        (
            "header_range",
            "header_range",
            _sub("xl/tables/table1.xml", 'name="A11yTable1"', 'name="Table1"'),
            "A11yTable",
        ),
        (
            "header_disabled_table",
            "header_table",
            _sub("xl/tables/table1.xml", 'name="Roster"', 'name="Roster2"'),
            "unexplained semantic change in xl/tables/table1.xml",
        ),
    ],
)
def test_comparator_rejects_unapproved_or_missing_changes(
    fixture: str, plan: str, change: Callable[[dict[str, bytes]], None], expected: str
) -> None:
    source = phase4_fixtures()[f"{fixture}.xlsx"]
    output = _rewrite(FakePhase4().edit(source, _decisions(fixture, plan)), change)
    problems = compare_packages(source, output, _effects(fixture, plan))
    assert any(expected in problem for problem in problems), problems


def test_comparator_requires_the_approved_change_to_take_effect() -> None:
    source = phase4_fixtures()["alt_supported.xlsx"]
    problems = compare_packages(source, source, _effects("alt_supported", "alt"))
    assert any("did not take effect" in problem for problem in problems)


# --- full runs against the fake target --------------------------------------------------


def run_phase4(tmp_path: Path, fake: FakePhase4) -> dict[str, Any]:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    settings = SettingsForTester(
        tester_db_path=tmp_path / "tester.sqlite3",
        allowed_target_urls=[BASE_URL],
        TARGET_AUTH_MAP_JSON=access_map(),
        TESTER_OPERATOR_TOKEN_HASHES_JSON={hashlib.sha256(OPERATOR.encode()).hexdigest(): "op"},
        fixture_manifest_path=MANIFEST,
    )
    services = ServicesForTester(
        settings=settings,
        transport=fake.transport(),
        resolver=address_resolver(),
        sleep=clock.sleep,
        clock=clock,
    )
    headers = {"Authorization": f"Bearer {OPERATOR}"}
    with TestClient(create_app(services=services)) as client:
        setup = client.post("/test/setup", json={"target_url": BASE_URL}, headers=headers)
        assert setup.status_code == 201, setup.text
        session_id = setup.json()["session_id"]
        run = client.post("/test/run", json={"session_id": session_id}, headers=headers)
        assert run.status_code == 200, run.text
        stored = client.get("/test/report", params={"session_id": session_id}, headers=headers)
        assert stored.content == run.content
    report: dict[str, Any] = run.json()
    return report


def outcomes(report: dict[str, Any]) -> dict[str, str]:
    return {result["test_id"]: result["outcome"] for result in report["results"]}


def test_full_phase4_run_against_a_conforming_fake(tmp_path: Path) -> None:
    report = run_phase4(tmp_path, FakePhase4(**SMALL))
    contract.validate("SuiteReport", report)
    counts = Counter(result["outcome"] for result in report["results"])
    assert counts == {"pass": 215}, [r for r in report["results"] if r["outcome"] != "pass"]
    assert report["coverage"]["component"]["covered"] == 8
    assert report["coverage"]["component"]["total"] == 9
    assert (report["coverage"]["system"]["covered"], report["coverage"]["system"]["total"]) == (
        11,
        11,
    )
    assert report["coverage"]["code"]["covered"] == 120
    manifest = SuiteManifest.model_validate_json(MANIFEST.read_bytes())
    record = run_record(
        json.dumps(report).encode(), manifest, tester_url="http://tester", target_revision="fake"
    )
    rows = {row["number"]: row for row in record["operation_status"]}
    assert rows["S"]["implemented"] == "observed responding"
    assert rows[11]["implemented"] == "not delivered in Phase 4"
    assert record["specification_version"] == "2.1.0"


@pytest.mark.parametrize(
    ("options", "failing", "skipped"),
    [
        ({"duplicate_setup_extra_field": True}, {"P3-SESSION-DUPLICATE"}, set()),
        (
            {"keep_applied_finding": True},
            {"P4-DECISIONS-MIXED", "P4-APPLY-ALT", "P4-APPLY-RENAME"},
            set(),
        ),
        ({"accept_manual_apply": True}, {"P4-REJECT-MANUAL-APPLY"}, set()),
        (
            {"fault_switch": False},
            {"P4-FAULT-SETUP"},
            {"P3-AUDIT-FAILURE-ENVELOPE", "P4-FAULT-RECOVERY"},
        ),
        (
            {
                "mutate_output": lambda parts: parts.__setitem__(
                    "docProps/app.xml", b"<Properties/>"
                )
            },
            {"P4-PRESERVATION", "P4-APPLY-ALT"},
            set(),
        ),
        (
            {"reviewer_grants": {}},
            {"P3-VERSION-REVIEWER", "P4-ACCESS-SOURCE-REVIEWER-READ", "P4-REVIEW-REVIEWER"},
            set(),
        ),
    ],
)
def test_defective_targets_fail_the_relevant_cases(
    tmp_path: Path, options: dict[str, Any], failing: set[str], skipped: set[str]
) -> None:
    report = run_phase4(tmp_path, FakePhase4(**SMALL, **options))
    result = outcomes(report)
    assert {test_id for test_id in failing if result[test_id] == "fail"} == failing
    assert {test_id for test_id in skipped if result[test_id] == "skip"} == skipped
    # A defect fails its own cases; it never turns into tester errors or aborts the run.
    assert "error" not in result.values()
    assert result["P3-HEALTH"] == "pass" and result["P4-REPORT-FORMAT"] == "pass"


def test_corrupt_output_zip_fails_cases_without_aborting(tmp_path: Path) -> None:
    result = outcomes(run_phase4(tmp_path, FakePhase4(**SMALL, corrupt_output=True)))
    assert result["P4-DECISIONS-MIXED"] == "fail"
    assert result["P4-PRESERVATION"] == "fail"
    assert "error" not in result.values()
    assert result["P4-FAULT-INVALID"] == "pass"


def test_inconsistent_coverage_counts_fail_without_aborting(tmp_path: Path) -> None:
    fake = FakePhase4(**SMALL, code_coverage={"covered": 5, "total": 3, "basis": "bad"})
    report = run_phase4(tmp_path, fake)
    result = outcomes(report)
    assert result["P4-SESSION-COVERAGE"] == "fail" and result["P3-CODE-REPORT"] == "fail"
    assert report["coverage"]["code"]["covered"] is None


def test_shared_audit_failure_skips_dependents_quickly(tmp_path: Path) -> None:
    fake = FakePhase4(**SMALL, fail_audits_for={"mixed_decisions.xlsx"})
    report = run_phase4(tmp_path, fake)
    result = outcomes(report)
    assert result["P4-REVIEW-CONTEXT"] == "fail"
    access = [r for r in report["results"] if r["test_id"].startswith("P4-ACCESS-")]
    # The first case on the shared access base reports the real failure; the rest skip fast.
    assert [r["outcome"] for r in access].count("fail") == 1
    assert all(
        "failed earlier" in r["message"] or "no completed remediation" in r["message"]
        for r in access
        if r["outcome"] == "skip"
    )
    uploads = [r for r in fake.requests if r.url.path == "/api/v1/workbooks"]
    assert len(uploads) < 120


def test_fractional_second_timestamps_are_ordered_by_time(tmp_path: Path) -> None:
    assert contract.parse_timestamp("2026-10-29T16:00:00Z") < contract.parse_timestamp(
        "2026-10-29T16:00:00.123Z"
    )
    for bad in ("2026-10-29T16Z", "20261029T160000Z", "2026-W44-4T16:00:00Z"):
        with pytest.raises(ValueError):
            contract.parse_timestamp(bad)
    result = outcomes(run_phase4(tmp_path, FakePhase4(**SMALL, fractional_times=True)))
    assert set(result.values()) == {"pass"}


def _ns_variant(data: bytes) -> bytes:
    """Re-prefix the drawing namespace and add an mc:Ignorable list, as another serializer may."""

    def change(parts: dict[str, bytes]) -> None:
        text = parts["xl/drawings/drawing1.xml"].decode()
        text = text.replace("xdr:", "sd:").replace("xmlns:xdr=", "xmlns:sd=")
        parts["xl/drawings/drawing1.xml"] = text.encode()

    return _rewrite(data, change)


def test_comparator_tolerates_reprefixing_spacing_and_directory_entries() -> None:
    source = phase4_fixtures()["link_supported.xlsx"]
    output = FakePhase4().edit(source, _decisions("link_supported", "link"))

    def tolerated(parts: dict[str, bytes]) -> None:
        sheet = parts["xl/worksheets/sheet1.xml"].decode()
        parts["xl/worksheets/sheet1.xml"] = sheet.replace(
            '<c r="C1" t="inlineStr"><is><t>click here</t>',
            '<c r="C1" s="0" t="inlineStr"><is><t xml:space="preserve">click here</t>',
        ).encode()
        parts["xl/"] = b""

    assert (
        compare_packages(source, _rewrite(output, tolerated), _effects("link_supported", "link"))
        == []
    )
    alt = phase4_fixtures()["alt_supported.xlsx"]
    edited = _ns_variant(FakePhase4().edit(alt, _decisions("alt_supported", "alt")))
    assert compare_packages(alt, edited, _effects("alt_supported", "alt")) == []


def test_comparator_checks_namespace_declarations_and_compatibility_prefixes() -> None:
    source = phase4_fixtures()["alt_supported.xlsx"]
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    x14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"

    def with_ignorable(prefix: str, declare: bool) -> Callable[[dict[str, bytes]], None]:
        def change(parts: dict[str, bytes]) -> None:
            text = parts["xl/drawings/drawing1.xml"].decode()
            extra = f' xmlns:mc="{mc}" mc:Ignorable="{prefix}"'
            if declare:
                extra += f' xmlns:{prefix}="{x14}"'
            parts["xl/drawings/drawing1.xml"] = text.replace(
                "<xdr:wsDr ", f"<xdr:wsDr{extra} ", 1
            ).encode()

        return change

    base = _rewrite(source, with_ignorable("x14", True))
    output = FakePhase4().edit(base, _decisions("alt_supported", "alt"))
    assert compare_packages(base, output, _effects("alt_supported", "alt")) == []
    renamed = _rewrite(
        output,
        _sub(
            "xl/drawings/drawing1.xml", 'mc:Ignorable="x14" xmlns:x14=', 'mc:Ignorable="y" xmlns:y='
        ),
    )
    assert compare_packages(base, renamed, _effects("alt_supported", "alt")) == []
    undeclared = _rewrite(output, _sub("xl/drawings/drawing1.xml", f' xmlns:x14="{x14}"', ""))
    problems = compare_packages(base, undeclared, _effects("alt_supported", "alt"))
    assert any("no longer declares namespace" in p for p in problems), problems
    assert any("undeclared prefix" in p for p in problems), problems


def test_comparator_allows_header_metadata_when_enabling_a_table_header() -> None:
    source = phase4_fixtures()["header_disabled_table.xlsx"]
    output = FakePhase4().edit(source, _decisions("header_disabled_table", "header_table"))
    with_filter = _rewrite(
        output,
        _sub(
            "xl/tables/table1.xml",
            'totalsRowShown="0">',
            'totalsRowShown="0"><autoFilter ref="A1:B4"/>',
        ),
    )
    assert (
        compare_packages(source, with_filter, _effects("header_disabled_table", "header_table"))
        == []
    )


def test_auxiliary_sessions_are_kept_then_cleaned_on_the_next_setup(tmp_path: Path) -> None:
    fake = FakePhase4(**SMALL)
    run_phase4(tmp_path, fake)
    from tester.store import TesterStore

    store = TesterStore(tmp_path / "tester.sqlite3")
    pending = store.cleanup_candidates(BASE_URL, failed_only=True)
    assert pending, "the fault and scratch namespaces are queued for deferred cleanup"
    assert all(session in fake.sessions or session.startswith("t9-") for session in pending)
    fault_sessions = [s for s in pending if s in fake.sessions]
    assert fault_sessions, "fault-session evidence must still exist after the run"
    run_phase4(tmp_path, fake)  # same tester database: the next setup compensates
    setups = [json.loads(r.content) for r in fake.requests if r.url.path == "/test/setup"]
    assert any(
        set(fault_sessions) <= set(body.get("setup", {}).get("cleanup_session_ids", []))
        for body in setups
    )
    assert not set(fault_sessions) & set(fake.sessions)


def test_skipped_cases_and_setup_calls_earn_no_coverage(tmp_path: Path) -> None:
    # Without the fault switch, cases depending on the fault session skip; with every review
    # grant removed the reviewer cases fail but still count. Coverage must stay honest.
    report = run_phase4(tmp_path, FakePhase4(**SMALL, fault_switch=False))
    assert outcomes(report)["P3-AUDIT-FAILURE-ENVELOPE"] == "skip"
    from app.contracts.models import CoverageMeasure, Outcome, TestResult
    from tester.suite.manifest import SuiteManifest as Manifest
    from tester.suite.manifest import load_suite as load
    from tester.suite.runner import build_report

    suite = load(MANIFEST, check_names())
    results = [
        TestResult(test_id=t.test_id, outcome=Outcome.SKIP, message="Not executed: test.")
        for t in suite.manifest.tests
    ]
    markers = {
        t.test_id: {"components": t.components, "acceptance": t.acceptance}
        for t in suite.manifest.tests
    }
    code = CoverageMeasure(covered=None, total=None, basis="n/a")
    empty = build_report(suite, "s", BASE_URL, results, code, markers)
    assert (empty.coverage.component.covered, empty.coverage.system.covered) == (0, 0)
    assert Manifest  # imported for type clarity


def test_a_preservation_defect_does_not_skip_independent_cases(tmp_path: Path) -> None:
    def mutate(parts: dict[str, bytes]) -> None:
        parts["docProps/app.xml"] = b"<Properties/>"

    result = outcomes(run_phase4(tmp_path, FakePhase4(**SMALL, mutate_output=mutate)))
    assert result["P4-DECISIONS-MIXED"] == "fail" and result["P4-PRESERVATION"] == "fail"
    for independent in (
        "P4-OUTPUT-AUDIT",
        "P4-REPORT-COMPLETE",
        "P4-REPORT-STABLE",
        "P4-HISTORY",
        "P4-REMEDIATED-STATUS",
        "P4-ACCESS-OUTPUT-REVIEWER-READ",
        "P4-ACCESS-REMEDIATION-FOREIGN",
    ):
        assert result[independent] == "pass", independent


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            _sub(
                "xl/worksheets/sheet1.xml",
                '<tableParts count="1">',
                '<tableParts count="7">',
            ),
            "tableParts count",
        ),
        (
            _sub("xl/tables/table1.xml", '<tableColumns count="3">', '<tableColumns count="9">'),
            "tableColumns count",
        ),
    ],
)
def test_comparator_requires_valid_table_part_counts(
    change: Callable[[dict[str, bytes]], None], expected: str
) -> None:
    source = phase4_fixtures()["header_range.xlsx"]
    output = _rewrite(FakePhase4().edit(source, _decisions("header_range", "header_range")), change)
    problems = compare_packages(source, output, _effects("header_range", "header_range"))
    assert any(expected in problem for problem in problems), problems


def test_comparator_requires_table_parts_at_the_end_of_the_worksheet() -> None:
    source = phase4_fixtures()["header_range.xlsx"]
    output = FakePhase4().edit(source, _decisions("header_range", "header_range"))

    def move(parts: dict[str, bytes]) -> None:
        text = parts["xl/worksheets/sheet1.xml"].decode()
        block = '<tableParts count="1"><tablePart r:id="rId1"/></tableParts>'
        text = text.replace(block, "").replace("<sheetData>", block + "<sheetData>")
        parts["xl/worksheets/sheet1.xml"] = text.encode()

    problems = compare_packages(
        source, _rewrite(output, move), _effects("header_range", "header_range")
    )
    assert any("last worksheet child" in problem for problem in problems), problems


def test_comparator_resolves_compatibility_prefixes_in_scope() -> None:
    source = phase4_fixtures()["alt_supported.xlsx"]
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    x14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"

    def declare_on_root(parts: dict[str, bytes]) -> None:
        text = parts["xl/drawings/drawing1.xml"].decode()
        extra = f' xmlns:mc="{mc}" mc:Ignorable="x14" xmlns:x14="{x14}"'
        parts["xl/drawings/drawing1.xml"] = text.replace(
            "<xdr:wsDr ", f"<xdr:wsDr{extra} ", 1
        ).encode()

    base = _rewrite(source, declare_on_root)
    output = FakePhase4().edit(base, _decisions("alt_supported", "alt"))

    def move_into_child(parts: dict[str, bytes]) -> None:
        text = parts["xl/drawings/drawing1.xml"].decode()
        text = text.replace(f' xmlns:x14="{x14}"', "", 1)
        text = text.replace("<xdr:twoCellAnchor ", f'<xdr:twoCellAnchor xmlns:x14="{x14}" ', 1)
        parts["xl/drawings/drawing1.xml"] = text.encode()

    problems = compare_packages(
        base, _rewrite(output, move_into_child), _effects("alt_supported", "alt")
    )
    assert any("undeclared prefix" in problem for problem in problems), problems


def test_pathological_json_is_a_contract_violation_not_a_crash() -> None:
    with pytest.raises(contract.ContractViolation):
        contract.parse_json(b"[" * 200_000 + b"]" * 200_000, "SetupResponse")
    with pytest.raises(contract.ContractViolation):
        contract.parse_json(b'{"session_id": ' + b"9" * 5000 + b"}", "SetupResponse")
