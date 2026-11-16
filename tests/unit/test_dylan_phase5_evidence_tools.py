"""Phase 5 evidence tooling must report what really happened and never invent results.

Covers the AC-18 harness outcome classification (successful audits versus controlled
failures), the precision study's refusal to score unadjudicated or altered findings, the
Excel inspection packet's automated fields and PENDING human fields, and the rename
inventory's reading of the shared rename fixture.
"""

import csv
import json
from pathlib import Path

import pytest

from scripts import ac18_performance, excel_inspection_packet, real_workbook_precision
from scripts.rename_inventory import apply_rename, describe

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "tests" / "fixtures" / "phase4" / "synthetic"


def small_workload(monkeypatch: pytest.MonkeyPatch) -> None:
    data = (SYNTHETIC / "mixed_decisions.xlsx").read_bytes()
    monkeypatch.setitem(ac18_performance.WORKLOADS, "small", (lambda: data, "test fixture"))


def test_ac18_harness_times_successful_runs_and_compares_findings(monkeypatch) -> None:
    small_workload(monkeypatch)
    result = ac18_performance.measure("small", "ordinary", runs=2, concurrency=2)
    assert result["settings"]["productionBounds"] is True
    assert result["summary"]["outcomes"] == {
        "SUCCESS": 2,
        "CONTROLLED_FAILURE": 0,
        "UNCONTROLLED": 0,
    }
    assert result["summary"]["successfulFindingsIdentical"] is True
    for run in result["runs"]:
        assert run["outcome"] == "SUCCESS" and run["ac18Satisfied"]
        for key in ("intakeValidationSeconds", "queueSeconds", "engineSeconds", "workerJobSeconds"):
            assert run[key] is not None and run[key] >= 0
        assert run["endToEndSeconds"] >= run["engineSeconds"]
        assert "_findings" not in run


def test_ac18_harness_reports_timeouts_as_controlled_failures_not_success(monkeypatch) -> None:
    small_workload(monkeypatch)
    result = ac18_performance.measure(
        "small", "ordinary", runs=1, concurrency=1, audit_timeout_seconds=0.001
    )
    run = result["runs"][0]
    assert result["settings"]["productionBounds"] is False
    assert run["outcome"] == "CONTROLLED_FAILURE" and run["auditStatus"] == "PROCESSING_FAILED"
    assert run["errorCode"] == "PROCESSING_TIMEOUT"
    assert "findingsDigest" not in run
    assert result["summary"]["outcomes"]["SUCCESS"] == 0


def test_precision_study_refuses_to_score_until_every_finding_is_adjudicated(tmp_path) -> None:
    names = [
        "mixed_decisions",
        "seeded_all_six",
        "alt_supported",
        "link_supported",
        "merge_vectors",
    ]
    workbooks = [(f"W{i}", SYNTHETIC / f"{name}.xlsx") for i, name in enumerate(names, 1)]
    assert real_workbook_precision.prepare(workbooks, tmp_path, repeats=2) == 0
    study = json.loads((tmp_path / "study.json").read_text())
    assert all(entry["identicalAcrossRepeats"] for entry in study["workbooks"])
    assert not any(str(path) in json.dumps(study) for _, path in workbooks)
    assert real_workbook_precision.score(tmp_path) == 2  # nothing adjudicated yet

    sheet = tmp_path / "adjudication.csv"
    with sheet.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    for index, row in enumerate(rows):
        # Synthetic verdicts exercise the arithmetic only; they are not a study result.
        row["verdict"] = "FP" if row["ruleId"] == "COLOR_CONTRAST" and index % 2 else "TP"
        row["excelChecker"] = "NOT_CHECKED_BY_TOOL"
        row["adjudicator"] = "unit-test"
    with sheet.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    real_workbook_precision.score(tmp_path)
    result = json.loads((tmp_path / "results.json").read_text())
    total = len(rows)
    false_positives = sum(1 for row in rows if row["verdict"] == "FP")
    assert result["overall"] == {
        "tp": total - false_positives,
        "fp": false_positives,
        "precision": (total - false_positives) / total,
    }
    for rule, item in result["rules"].items():
        if not any(row["ruleId"] == rule for row in rows):
            assert item["precision"] is None and item["estimable"] is False

    rows[0]["message"] = "edited"
    with sheet.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(SystemExit, match="altered"):
        real_workbook_precision.score(tmp_path)


def test_excel_packet_records_automated_results_and_leaves_human_fields_pending(
    tmp_path: Path,
) -> None:
    plan = next(p for p in excel_inspection_packet.load_plans() if p["id"] == "alt_supported__alt")
    with excel_inspection_packet.local_system(tmp_path) as client:
        automated, output = excel_inspection_packet.run_plan(client, plan)
    assert output is not None
    assert automated["remediationStatus"] == "REMEDIATED"
    assert automated["independentComparator"]["result"] == "PASS"
    assert automated["outputSha256"] == automated["declaredOutputSha256"]
    assert set(excel_inspection_packet.manual_fields().values()) <= {"PENDING", ""}


def test_rename_inventory_reads_the_shared_rename_fixture() -> None:
    data = (SYNTHETIC / "rename_references.xlsx").read_bytes()
    report = describe(data, "rename_references.xlsx")
    (finding,) = report["worksheetNameFindings"]
    assert finding["classification"] == "REQUIRES_USER_INPUT"
    assert finding["referenceKinds"] == [
        "cell_formula",
        "chart_formula",
        "conditional_x14",
        "defined_name",
        "hyperlink_location",
        "validation",
    ]
    # The only conditional-formatting reference is in Excel's x14 extension, so reading
    # x14 as an unsupported formula extension would make this required rename manual.
    assert finding["strictX14Classification"] == "MANUAL_REVIEW"
    applied = apply_rename(data, "rename_references.xlsx", "Data source", None)
    assert applied["applied"] and applied["remainingOldNameSites"] == []
    assert len(applied["rewrittenReferenceSites"]) == len(finding["referenceSites"])
