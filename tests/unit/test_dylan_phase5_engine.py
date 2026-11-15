"""Phase 5: scheduled and manual re-audits reuse one deterministic engine.

Specification 3.0.0 §7 requires scheduled audits to use "the same engine, status
derivation, authorization inheritance, failure containment and provenance rules as
manual audits", to create a fresh audit record and to preserve every older audit and
report. These tests pin the engine-side half of that promise:

* the six-rule evaluation of fixed bytes is identical in every process, whatever the
  interpreter's string-hash seed (each audit runs in a fresh bounded child process);
* engine modules read no wall clock, randomness, locale or environment, so a simulated
  namespace clock cannot change findings;
* through the real API, SQLite, filesystem storage and worker children, a re-audit of a
  stored version reproduces the earlier findings exactly while the earlier audit, its
  downloaded report bytes and the stored source bytes stay unchanged, including across a
  ruleset 1.0.0 -> 2.0.0 upgrade of retained data.

The scheduler-driven variant lives in test_dylan_phase5_scheduled_audit.py.
"""

import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit.engine import OoxmlWorkbookEngine
from app.config import SystemSettings
from app.contracts.models import Principal, ProcessingLimits
from app.jobs.worker import AuditWorker
from app.main import create_app
from app.services.assembly import assemble

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "tests" / "fixtures" / "phase4" / "synthetic"
RECEIVED = ROOT / "tests" / "fixtures" / "phase4" / "received" / "team12" / "clean.xlsx"
AUTH = {"Authorization": "Bearer owner"}
# The 3 MB preservation fixture adds runtime without adding rule coverage here.
DETERMINISM_FIXTURES = sorted(
    path for path in SYNTHETIC.glob("*.xlsx") if path.name != "large_preservation.xlsx"
) + [RECEIVED]


def evaluation_digests(paths: list[Path]) -> dict[str, str]:
    engine = OoxmlWorkbookEngine()
    digests = {}
    for path in paths:
        data = path.read_bytes()
        validated = engine.validate(data, path.name, ProcessingLimits())
        evaluation = engine.audit(data, validated, "2.0.0")
        canonical = evaluation.model_dump_json().encode()
        digests[path.name] = hashlib.sha256(canonical).hexdigest()
    return digests


CHILD = """
import json, sys
from pathlib import Path
from tests.unit.test_dylan_phase5_engine import evaluation_digests
print(json.dumps(evaluation_digests([Path(p) for p in sys.argv[1:]])))
"""


@pytest.mark.parametrize("seed", ["0", "1", "4294967295"])
def test_six_rule_evaluation_is_identical_across_processes_and_hash_seeds(seed: str) -> None:
    """Every audit runs in a fresh child; string-hash order must never reach the output."""
    expected = evaluation_digests(DETERMINISM_FIXTURES)
    environment = {
        "PATH": os.defpath,
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": str(ROOT),
        "PYTHONNOUSERSITE": "1",
        "LANG": "C.UTF-8",
    }
    completed = subprocess.run(
        [sys.executable, "-c", CHILD, *map(str, DETERMINISM_FIXTURES)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert json.loads(completed.stdout) == expected


def test_repeated_in_process_audits_are_identical() -> None:
    assert evaluation_digests(DETERMINISM_FIXTURES) == evaluation_digests(DETERMINISM_FIXTURES)


# Modules whose results could differ between a manual audit and a later scheduled
# audit of the same bytes. `hashlib` is deterministic and is allowed.
NONDETERMINISTIC_MODULES = frozenset(
    {"time", "datetime", "random", "secrets", "uuid", "os", "locale", "zoneinfo", "socket"}
)


def test_engine_modules_read_no_clock_randomness_locale_or_environment() -> None:
    offending = []
    for package in ("audit", "workbooks"):
        for path in sorted((ROOT / "app" / package).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "hash"
                ):
                    names = ["builtins.hash"]
                for name in names:
                    if name.split(".")[0] in NONDETERMINISTIC_MODULES or name == "builtins.hash":
                        offending.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    assert offending == []


# -- real API, SQLite, filesystem storage and isolated worker children ---------------


def settings_for(tmp_path: Path, ruleset: str) -> SystemSettings:
    return SystemSettings(
        _env_file=None,
        ruleset_version=ruleset,
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "sources",
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(b"owner").hexdigest(): Principal(subject="owner", role="user")
        },
        ui_router_factory=None,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    services = assemble(settings_for(tmp_path, "2.0.0"), engine=OoxmlWorkbookEngine())
    with TestClient(create_app(services=services)) as test:
        yield test


def drain(client: TestClient) -> None:
    worker = AuditWorker(client.app.state.services)
    assert worker.run_once()
    while worker.run_once():
        pass


def get(client: TestClient, path: str) -> dict:
    response = client.get(path, headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def fixture_bytes(name: str) -> bytes:
    return (SYNTHETIC / f"{name}.xlsx").read_bytes()


def expectations() -> dict:
    with (ROOT / "tests" / "fixtures" / "expectations-phase4.json").open() as handle:
        return json.load(handle)["fixtures"]


def comparable(audit: dict) -> dict:
    """Everything a re-audit must reproduce; identifiers and timestamps are per audit."""
    return {key: audit[key] for key in ("rulesetVersion", "status", "findings", "limitations")}


def reaudit(client: TestClient, workbook_id: str, version_id: str) -> dict:
    path = f"/api/v1/workbooks/{workbook_id}/versions/{version_id}"
    response = client.post(f"{path}/audits", json={}, headers=AUTH)
    assert response.status_code == 202, response.text
    drain(client)
    return get(client, f"{path}/audits/{response.json()['auditId']}")


def snapshot(client: TestClient, workbook_id: str, version_id: str, audit_id: str) -> dict:
    """Byte-level retained state of one audit: its record, report download and source.

    Review context is served only for the latest audit (older ones return STALE_AUDIT),
    and ruleset 1.0.0 audits never had a report snapshot, so status codes are kept too.
    """
    path = f"/api/v1/workbooks/{workbook_id}/versions/{version_id}"
    audit_path = f"{path}/audits/{audit_id}"
    audit = client.get(audit_path, headers=AUTH)
    source = client.get(f"{path}/source", headers=AUTH)
    report = client.get(f"{audit_path}/report", headers=AUTH)
    assert audit.status_code == 200 and source.status_code == 200
    assert report.status_code in (200, 409)
    if report.status_code == 409:
        assert report.json()["code"] == "LEGACY_REPORT_UNAVAILABLE"
    return {
        "audit": audit.content,
        "report": (report.status_code, report.content if report.status_code == 200 else b""),
        "source": source.content,
    }


def review_items(client: TestClient, workbook_id: str, version_id: str, audit_id: str) -> list:
    path = f"/api/v1/workbooks/{workbook_id}/versions/{version_id}/audits/{audit_id}/review"
    return get(client, path)["items"]


def test_manual_reaudits_reproduce_findings_and_keep_history_bytes(client: TestClient) -> None:
    data = fixture_bytes("mixed_decisions")
    accepted = client.post(
        "/api/v1/workbooks", files={"file": ("mixed.xlsx", data)}, headers=AUTH
    ).json()
    wid, vid, first_id = accepted["workbookId"], accepted["versionId"], accepted["auditId"]
    drain(client)
    path = f"/api/v1/workbooks/{wid}/versions/{vid}"
    first = get(client, f"{path}/audits/{first_id}")
    assert first["status"] == "REMEDIATION_AVAILABLE" and first["findings"]
    before = snapshot(client, wid, vid, first_id)
    assert before["source"] == data and before["report"][0] == 200
    first_review = review_items(client, wid, vid, first_id)

    repeats = []
    for _ in range(2):
        repeat = reaudit(client, wid, vid)
        assert repeat["auditId"] != first_id
        assert comparable(repeat) == comparable(first)
        assert review_items(client, wid, vid, repeat["auditId"]) == first_review
        repeats.append(repeat)

    assert snapshot(client, wid, vid, first_id) == before
    history = get(client, f"/api/v1/workbooks/{wid}/history")
    assert [item["versionId"] for item in history["versions"]] == [vid]
    assert [item["auditId"] for item in history["audits"]] == [
        first_id,
        *(item["auditId"] for item in repeats),
    ]
    assert get(client, path)["latestAuditId"] == repeats[-1]["auditId"]


def test_generated_version_reaudit_matches_its_publication_audit(client: TestClient) -> None:
    """A schedule follows the newest version, which can be a generated output."""
    fixture = expectations()["mixed_decisions"]
    data = fixture_bytes("mixed_decisions")
    accepted = client.post(
        "/api/v1/workbooks", files={"file": ("mixed.xlsx", data)}, headers=AUTH
    ).json()
    wid, vid = accepted["workbookId"], accepted["versionId"]
    drain(client)
    audit = get(client, f"/api/v1/workbooks/{wid}/versions/{vid}/audits/{accepted['auditId']}")
    ids = {(item["ruleId"], item["target"]): item["findingId"] for item in audit["findings"]}
    decisions = []
    for key in fixture["extras"]["plans"]["combined"]["decisions"]:
        spec = fixture["extras"]["decisions"][key]
        decision = {"findingId": ids[tuple(spec["finding"])], "action": spec["action"]}
        if "value" in spec:
            decision["value"] = spec["value"]
        decisions.append(decision)
    response = client.post(
        f"/api/v1/workbooks/{wid}/versions/{vid}/remediations",
        json={"auditId": accepted["auditId"], "decisions": decisions},
        headers=AUTH | {"Idempotency-Key": "dylan-phase5-generated"},
    )
    assert response.status_code == 202, response.text
    drain(client)
    status = get(client, f"/api/v1/remediations/{response.json()['remediationId']}")
    assert status["status"] == "REMEDIATED", status
    output_vid, published_id = status["outputVersionId"], status["auditId"]
    published = get(client, f"/api/v1/workbooks/{wid}/versions/{output_vid}/audits/{published_id}")
    before = snapshot(client, wid, output_vid, published_id)
    source_before = snapshot(client, wid, vid, accepted["auditId"])
    published_review = review_items(client, wid, output_vid, published_id)

    again = reaudit(client, wid, output_vid)
    assert comparable(again) == comparable(published)
    assert review_items(client, wid, output_vid, again["auditId"]) == published_review
    assert snapshot(client, wid, output_vid, published_id) == before
    assert snapshot(client, wid, vid, accepted["auditId"]) == source_before
    history = get(client, f"/api/v1/workbooks/{wid}/history")
    assert [item["versionId"] for item in history["versions"]] == [vid, output_vid]


def test_upgrade_reaudit_keeps_retained_ruleset_1_audit_and_report_bytes(tmp_path: Path) -> None:
    """Retained Phase 3 audits stay byte-identical when the installed ruleset changes."""
    data = fixture_bytes("seeded_all_six")
    legacy = assemble(settings_for(tmp_path, "1.0.0"), engine=OoxmlWorkbookEngine())
    with TestClient(create_app(services=legacy)) as old:
        accepted = old.post(
            "/api/v1/workbooks", files={"file": ("seeded.xlsx", data)}, headers=AUTH
        ).json()
        wid, vid, legacy_id = accepted["workbookId"], accepted["versionId"], accepted["auditId"]
        drain(old)
        legacy_audit = get(old, f"/api/v1/workbooks/{wid}/versions/{vid}/audits/{legacy_id}")
        assert legacy_audit["rulesetVersion"] == "1.0.0"
        before = snapshot(old, wid, vid, legacy_id)

    current = assemble(settings_for(tmp_path, "2.0.0"), engine=OoxmlWorkbookEngine())
    with TestClient(create_app(services=current)) as new:
        assert snapshot(new, wid, vid, legacy_id) == before
        upgraded = reaudit(new, wid, vid)
        assert upgraded["rulesetVersion"] == "2.0.0"
        assert {finding["ruleId"] for finding in upgraded["findings"]} > {
            finding["ruleId"] for finding in legacy_audit["findings"]
        }
        assert comparable(reaudit(new, wid, vid)) == comparable(upgraded)
        assert snapshot(new, wid, vid, legacy_id) == before
