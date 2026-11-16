"""Five-real-workbook precision study (P4-MANUAL-REAL-WORKBOOKS, P4-MANUAL-REAL-DETERMINISM,
P5-MANUAL-REAL-PRECISION; Phase 2 Q9 and spec 3.0.0 §17).

The study has two machine steps around one human step:

1. ``prepare`` audits each de-identified real workbook with the production engine
   through the production bounded child-process runner, five times per workbook, and
   records whether all five evaluations are identical. It writes ``study.json`` and an
   ``adjudication.csv`` with one row per finding and empty human columns, plus an empty
   ``checker_only.csv`` for Microsoft Accessibility Checker issues the engine did not
   report.
2. A person adjudicates every finding (``TP``: a real accessibility barrier correctly
   located and described; ``FP``: not a real barrier, or wrong location) and records
   what Microsoft's Accessibility Checker reported for the same item.
3. ``score`` refuses to score while any verdict is missing or any row was added,
   removed or altered, then reports per-rule and overall precision. A rule with no
   findings is "not estimable", never 100%. The checker comparison is supporting
   evidence only; it is not the oracle.

Workbook paths are never written to the study; only the labels you supply, hashes and
the engine's own output are. Findings can quote sheet names and short cell text, so use
de-identified workbooks and review the CSV before sharing or committing it.

    uv run python -m scripts.real_workbook_precision prepare --output study \\
        --workbook W1=/path/one.xlsx --workbook W2=/path/two.xlsx ...   # at least five
    uv run python -m scripts.real_workbook_precision score --study study
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.audit.engine import OoxmlWorkbookEngine
from app.config import SystemSettings
from app.contracts.errors import EngineExecutionError
from app.contracts.models import ProcessingLimits
from app.contracts.phase4 import RULE_IDS
from app.jobs.execution import execute_bounded
from app.services.assembly import load_factory
from scripts.engine_evidence import environment, findings_digest, git_revision, sha256

REQUIRED_WORKBOOKS = 5
REPEATS = 5
THRESHOLD = 0.90
VERDICTS = {"TP", "FP"}
CHECKER = {"FLAGGED", "NOT_FLAGGED", "NOT_CHECKED_BY_TOOL"}
AUTOMATED_COLUMNS = [
    "label",
    "findingId",
    "ruleId",
    "classification",
    "worksheet",
    "target",
    "message",
]
HUMAN_COLUMNS = ["verdict", "excelChecker", "adjudicator", "notes"]
CHECKER_ONLY_COLUMNS = [
    "label",
    "checkerIssue",
    "location",
    "realBarrier",
    "closestRule",
    "adjudicator",
    "notes",
]


def audit_repeatedly(data: bytes, filename: str, repeats: int) -> dict[str, Any]:
    """Validate and audit exactly as the worker does: a fresh bounded child each time."""
    settings = SystemSettings(_env_file=None)
    assert settings.runner_factory is not None
    runner = load_factory(settings.runner_factory)(
        engine=OoxmlWorkbookEngine(), enable_code_measurement=False
    )
    limits = ProcessingLimits()
    try:
        validation = execute_bounded(
            runner, "validate", data, filename, limits, "2.0.0", None, None, "precision", 30, 512
        )
    except EngineExecutionError as exc:
        return {"accepted": False, "rejection": exc.code}
    validated = validation.validated
    assert validated is not None
    runs: list[dict[str, Any]] = []
    for _ in range(repeats):
        try:
            result = execute_bounded(
                runner,
                "audit",
                data,
                filename,
                limits,
                "2.0.0",
                validated,
                None,
                "precision",
                60,
                512,
            )
        except EngineExecutionError as exc:
            runs.append({"error": exc.code})
            continue
        assert result.evaluation is not None
        evaluation = result.evaluation.model_dump(mode="json")
        runs.append(
            {
                "digest": findings_digest(evaluation["findings"], evaluation["limitations"]),
                "evaluation": evaluation,
            }
        )
    digests = [run.get("digest") for run in runs]
    successful = [run for run in runs if "digest" in run]
    return {
        "accepted": True,
        "worksheetCount": validated.worksheet_count,
        "nonemptyCellCount": validated.nonempty_cell_count,
        "repeats": repeats,
        "runErrors": [run["error"] for run in runs if "error" in run],
        "digests": digests,
        "identicalAcrossRepeats": len(successful) == repeats and len(set(digests)) == 1,
        "evaluation": successful[0]["evaluation"] if successful else None,
    }


def prepare(workbooks: list[tuple[str, Path]], output: Path, repeats: int) -> int:
    labels = [label for label, _ in workbooks]
    if len(set(labels)) != len(labels):
        raise SystemExit("Workbook labels must be unique.")
    output.mkdir(parents=True, exist_ok=True)
    entries, rows = [], []
    for label, path in workbooks:
        data = path.read_bytes()
        result = audit_repeatedly(data, "workbook.xlsx", repeats)
        evaluation = result.pop("evaluation", None)
        entry: dict[str, Any] = {"label": label, "sha256": sha256(data), "bytes": len(data)}
        entry |= result
        if evaluation is not None:
            entry["findingCount"] = len(evaluation["findings"])
            entry["findingsByRule"] = dict(Counter(f["ruleId"] for f in evaluation["findings"]))
            entry["limitations"] = evaluation["limitations"]
            for finding in evaluation["findings"]:
                rows.append({"label": label} | {k: finding[k] for k in AUTOMATED_COLUMNS[1:]})
        entries.append(entry)
    with (output / "adjudication.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, AUTOMATED_COLUMNS + HUMAN_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row | dict.fromkeys(HUMAN_COLUMNS, ""))
    checker_only = output / "checker_only.csv"
    if not checker_only.exists():
        with checker_only.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(CHECKER_ONLY_COLUMNS)
    study = {
        "kind": "real-workbook-precision-study",
        "rulesetVersion": "2.0.0",
        "preparedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "revision": git_revision(),
        "environment": environment(),
        "runner": "production bounded child-process runner, code measurement disabled",
        "workbooks": entries,
        # Binds the CSV to the engine output so edits to automated columns are detected.
        "findingRowsDigest": sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ),
    }
    (output / "study.json").write_text(
        json.dumps(study, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    accepted = sum(1 for e in entries if e["accepted"])
    unstable = [e["label"] for e in entries if e["accepted"] and not e["identicalAcrossRepeats"]]
    print(
        f"{len(rows)} findings from {accepted} accepted workbook(s) written to "
        f"{output / 'adjudication.csv'}; nonidentical repeats: {unstable or 'none'}"
    )
    if accepted < REQUIRED_WORKBOOKS:
        print(f"NOTE: the study needs at least {REQUIRED_WORKBOOKS} accepted real workbooks.")
    return 1 if unstable else 0


def _precision(tp: int, fp: int) -> float | None:
    return None if tp + fp == 0 else tp / (tp + fp)


def score(study_dir: Path) -> int:
    study = json.loads((study_dir / "study.json").read_text(encoding="utf-8"))
    with (study_dir / "adjudication.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    automated = [{key: row[key] for key in AUTOMATED_COLUMNS} for row in rows]
    digest = sha256(json.dumps(automated, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if digest != study["findingRowsDigest"]:
        raise SystemExit("adjudication.csv rows were added, removed, reordered or altered.")
    pending = [r["findingId"] for r in rows if r["verdict"].strip().upper() not in VERDICTS]
    bad_checker = [
        r["findingId"]
        for r in rows
        if r["excelChecker"].strip() and r["excelChecker"].strip().upper() not in CHECKER
    ]
    unattributed = [r["findingId"] for r in rows if r["verdict"].strip() and not r["adjudicator"]]
    if pending or bad_checker or unattributed:
        print(
            f"Not scored: {len(pending)} finding(s) lack a TP/FP verdict, {len(bad_checker)} "
            f"have an invalid checker value, {len(unattributed)} lack an adjudicator."
        )
        return 2
    per_rule: dict[str, Counter[str]] = defaultdict(Counter)
    checker: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        per_rule[row["ruleId"]][row["verdict"].strip().upper()] += 1
        checker[row["ruleId"]][row["excelChecker"].strip().upper() or "NOT_RECORDED"] += 1
    rules: dict[str, dict[str, Any]] = {}
    for rule in RULE_IDS:
        tp, fp = per_rule[rule]["TP"], per_rule[rule]["FP"]
        value = _precision(tp, fp)
        rules[rule] = {
            "tp": tp,
            "fp": fp,
            "precision": value,
            "estimable": value is not None,
            "meetsThreshold": None if value is None else value >= THRESHOLD,
            "excelChecker": dict(checker[rule]),
        }
    tp = sum(r["tp"] for r in rules.values())
    fp = sum(r["fp"] for r in rules.values())
    overall = _precision(tp, fp)
    with (study_dir / "checker_only.csv").open(newline="", encoding="utf-8") as handle:
        checker_only = list(csv.DictReader(handle))
    accepted = [w for w in study["workbooks"] if w["accepted"]]
    complete = (
        len(accepted) >= REQUIRED_WORKBOOKS
        and all(w["identicalAcrossRepeats"] for w in accepted)
        and overall is not None
    )
    passed = complete and all(r["meetsThreshold"] is not False for r in rules.values())
    passed = passed and overall is not None and overall >= THRESHOLD
    result = {
        "scoredAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "threshold": THRESHOLD,
        "acceptedWorkbooks": len(accepted),
        "allRepeatsIdentical": all(w["identicalAcrossRepeats"] for w in accepted),
        "overall": {"tp": tp, "fp": fp, "precision": overall},
        "rules": rules,
        "checkerOnlyIssues": len(checker_only),
        "checkerOnlyRealBarriers": sum(
            1 for row in checker_only if row["realBarrier"].strip().upper() in ("Y", "YES")
        ),
        "adjudicators": sorted({row["adjudicator"] for row in rows}),
        "studyComplete": complete,
        "meetsPrecisionCriterion": passed,
    }
    (study_dir / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--workbook", action="append", required=True, metavar="LABEL=PATH")
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--repeats", type=int, default=REPEATS)
    scoring = commands.add_parser("score")
    scoring.add_argument("--study", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        workbooks = []
        for item in args.workbook:
            label, separator, path = item.partition("=")
            if not separator or not label or not path:
                parser.error("--workbook takes LABEL=PATH")
            workbooks.append((label, Path(path)))
        return prepare(workbooks, args.output, args.repeats)
    return score(args.study)


if __name__ == "__main__":
    sys.exit(main())
