"""Prepare the Microsoft Excel no-repair inspection packet (AC-09, spec 3.0.0 §17).

For every supported correction plan in the independent Phase 4 fixture pack
(tests/fixtures/expectations-phase4.json), this script:

1. verifies the reviewed fixture bytes against their recorded SHA256;
2. uploads them to a private local instance of the real system (API, SQLite, filesystem
   storage, production engine and bounded worker children), audits them, submits the
   plan's exact decisions and downloads the published output workbook;
3. compares source and output with the tester's independent package comparator against
   the plan's approved effects, and records the output audit;
4. writes the output workbooks, a JSON manifest and a Markdown inspection record whose
   human fields are all PENDING.

It never opens Excel and never marks an inspection as done. A person must open each
output in Microsoft Excel, observe whether a repair prompt appears, check the listed
effects and fill in the record (P4-MANUAL-EXCEL-*, P5-MANUAL-EXCEL).

    uv run python -m scripts.excel_inspection_packet --output docs/evidence/phase5/excel_inspection
"""

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.engine_evidence import (
    OWNER,
    ROOT,
    drain,
    environment,
    git_revision,
    local_system,
    sha256,
)
from tester.phase4.package import compare_packages

FIXTURES = ROOT / "tests" / "fixtures"
#: Files above this size are regenerated from the manifest command instead of committed.
COMMIT_LIMIT = 1_000_000

FAMILY_TESTS = {
    "alt": "P4-MANUAL-EXCEL-ALT",
    "rename": "P4-MANUAL-EXCEL-RENAME",
    "link": "P4-MANUAL-EXCEL-LINK",
    "header_range": "P4-MANUAL-EXCEL-HEADER",
    "header_table": "P4-MANUAL-EXCEL-HEADER",
}

CHECKS = {
    "alt": [
        "Select the object, open Alt Text (right-click > View Alt Text / Edit Alt Text) and "
        "confirm the approved description. Current Excel builds may not display the title; "
        "record what the pane shows.",
        "The object's position, size, image and chart content look unchanged.",
    ],
    "link": [
        "The cell displays the approved label text.",
        "Edit Hyperlink (or hover) shows the original, unchanged destination.",
    ],
    "header_range": [
        "Clicking inside the range shows the Table tab; the table covers exactly the "
        "approved range and its first row is the header row.",
        "Header labels and every body value/formula result are unchanged.",
    ],
    "header_table": [
        "The existing table now shows its header row with the expected column names.",
        "Body values and the table's range are unchanged.",
    ],
    "rename": [
        "The sheet tab shows the approved new name.",
        "Formulas, Name Manager entries, chart Select Data series, Data Validation sources, "
        "Conditional Formatting > Manage Rules formulas and internal hyperlinks point to the "
        "renamed sheet and still show the same values (no #REF!).",
        "Ordinary text, string literals and similarly named sheets are unchanged.",
    ],
}


def load_plans() -> list[dict[str, Any]]:
    expectations = json.loads((FIXTURES / "expectations-phase4.json").read_text())["fixtures"]
    plans = []
    for fixture_id, fixture in expectations.items():
        extras = fixture.get("extras") or {}
        for plan_id, plan in sorted((extras.get("plans") or {}).items()):
            effects = plan.get("effects", [])
            families = sorted({effect["type"] for effect in effects})
            decisions = [extras["decisions"][key] for key in plan["decisions"]]
            plans.append(
                {
                    "id": f"{fixture_id}__{plan_id}",
                    "fixture": fixture_id,
                    "plan": plan_id,
                    "sourcePath": f"tests/fixtures/{fixture['file']}",
                    "sourceSha256": fixture["sha256"],
                    "uploadName": fixture.get("upload_name") or Path(fixture["file"]).name,
                    "families": families,
                    "decisions": decisions,
                    "effects": effects,
                }
            )
    return plans


def run_plan(client: Any, plan: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    source = (ROOT / plan["sourcePath"]).read_bytes()
    if sha256(source) != plan["sourceSha256"]:
        raise SystemExit(f"{plan['sourcePath']} differs from its reviewed SHA256")
    accepted = client.post(
        "/api/v1/workbooks", files={"file": (plan["uploadName"], source)}, headers=OWNER
    ).json()
    drain(client)
    wid, vid, aid = accepted["workbookId"], accepted["versionId"], accepted["auditId"]
    audit = client.get(f"/api/v1/workbooks/{wid}/versions/{vid}/audits/{aid}", headers=OWNER)
    ids = {(f["ruleId"], f["target"]): f["findingId"] for f in audit.json()["findings"]}
    body = []
    for decision in plan["decisions"]:
        item = {"findingId": ids[tuple(decision["finding"])], "action": decision["action"]}
        if "value" in decision:
            item["value"] = decision["value"]
        body.append(item)
    response = client.post(
        f"/api/v1/workbooks/{wid}/versions/{vid}/remediations",
        json={"auditId": aid, "decisions": body},
        headers=OWNER | {"Idempotency-Key": f"excel-{plan['id']}"},
    )
    result: dict[str, Any] = {"remediationAccepted": response.status_code}
    if response.status_code != 202:
        result["error"] = response.json()
        return result, None
    drain(client)
    status = client.get(
        f"/api/v1/remediations/{response.json()['remediationId']}", headers=OWNER
    ).json()
    result["remediationStatus"] = status["status"]
    if status["status"] != "REMEDIATED":
        result["error"] = status.get("error")
        return result, None
    output = client.get(f"/api/v1/outputs/{status['outputId']}/workbook", headers=OWNER).content
    version = client.get(
        f"/api/v1/workbooks/{wid}/versions/{status['outputVersionId']}", headers=OWNER
    ).json()
    output_audit = client.get(
        f"/api/v1/workbooks/{wid}/versions/{status['outputVersionId']}/audits/{status['auditId']}",
        headers=OWNER,
    ).json()
    problems = compare_packages(source, output, plan["effects"])
    result |= {
        "outputSha256": sha256(output),
        "declaredOutputSha256": version["sha256"],
        "outputBytes": len(output),
        "identicalToSource": output == source,
        "independentComparator": {
            "tool": "tester.phase4.package.compare_packages",
            "problems": problems,
            "result": "PASS" if not problems else "FAIL",
        },
        "outputAudit": {
            "status": output_audit["status"],
            "findings": [
                [f["ruleId"], f["worksheet"], f["target"], f["classification"]]
                for f in output_audit["findings"]
            ],
        },
    }
    return result, output


def manual_fields() -> dict[str, str]:
    return {
        "excelVersion": "PENDING",
        "platform": "PENDING",
        "reviewer": "PENDING",
        "date": "PENDING",
        "openedWithoutRepairPrompt": "PENDING",
        "approvedEffectsObserved": "PENDING",
        "unexplainedChangesObserved": "PENDING",
        "notes": "",
    }


def record_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# Excel no-repair inspection record (AC-09)",
        "",
        "Generated by `scripts.excel_inspection_packet`. Automated fields were produced by the "
        "real local system; **every human field is PENDING until a person performs the check "
        "in Microsoft Excel.** Do not edit automated fields.",
        "",
        f"- Generated: {manifest['generatedAt']}",
        f"- Code revision: `{manifest['revision']['commit']}` "
        f"(uncommitted paths at generation: {len(manifest['revision']['dirtyPaths'])})",
        f"- Host: {manifest['environment']['platform']}",
        "",
        "## How to inspect each output",
        "",
        "1. Record Excel's version (Excel > About Excel) and the OS version once.",
        "2. Open the output file read-only. Do **not** save it: saving changes the bytes.",
        "3. Record whether Excel shows *We found a problem with some content...* or any repair "
        "or Protected View error. Any repair prompt is a failure.",
        "4. Check the family-specific effects listed below, then close without saving.",
        "5. Confirm the file's SHA256 equals the recorded output hash "
        "(`shasum -a 256 FILE`) so the record refers to the exact bytes.",
        "",
    ]
    for item in manifest["outputs"]:
        auto = item["automated"]
        lines += [
            f"## {item['id']}",
            "",
            f"- Test IDs: {', '.join(item['testIds'])}",
            f"- Source: `{item['sourcePath']}` SHA256 `{item['sourceSha256']}`",
            f"- Output: `{item.get('outputFile') or 'not committed; regenerate'}` "
            f"SHA256 `{auto.get('outputSha256', 'n/a')}`",
            f"- Remediation: {auto.get('remediationStatus', auto['remediationAccepted'])}; "
            f"independent comparator: {auto.get('independentComparator', {}).get('result')}; "
            f"output audit: {auto.get('outputAudit', {}).get('status')}",
            "- Approved decisions: "
            + "; ".join(
                f"{d['action']} {d['finding'][0]} `{d['finding'][1]}`"
                + (f" = `{json.dumps(d['value'], ensure_ascii=False)}`" if "value" in d else "")
                for d in item["decisions"]
            ),
            "",
            "Check in Excel:",
            "",
        ]
        checks = [check for family in item["families"] for check in CHECKS.get(family, [])]
        if not checks:
            checks = ["No edit was approved; the output must be byte-identical to the source."]
        lines += [f"- [ ] {check}" for check in checks]
        lines += ["", "| Field | Value |", "|---|---|"]
        lines += [f"| {key} | {value or ' '} |" for key, value in item["manual"].items()]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    plans = load_plans()
    outputs = []
    with tempfile.TemporaryDirectory(prefix="excel-packet-") as state:
        with local_system(Path(state)) as client:
            for plan in plans:
                automated, output = run_plan(client, plan)
                entry = {
                    "id": plan["id"],
                    "testIds": sorted(
                        {FAMILY_TESTS[f] for f in plan["families"] if f in FAMILY_TESTS}
                        | {"P5-MANUAL-EXCEL"}
                    ),
                    "families": plan["families"],
                    "sourcePath": plan["sourcePath"],
                    "sourceSha256": plan["sourceSha256"],
                    "decisions": plan["decisions"],
                    "approvedEffects": plan["effects"],
                    "automated": automated,
                    "manual": manual_fields(),
                }
                if output is not None and len(output) <= COMMIT_LIMIT:
                    name = f"{plan['id']}.output.xlsx"
                    (args.output / name).write_bytes(output)
                    entry["outputFile"] = name
                outputs.append(entry)
    manifest = {
        "kind": "excel-no-repair-inspection-packet",
        "specificationVersion": "3.0.0",
        "rulesetVersion": "2.0.0",
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "revision": git_revision(),
        "environment": environment(),
        "system": "local real stack (in-process API, SQLite, filesystem storage, worker children)",
        "outputs": outputs,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (args.output / "INSPECTION_RECORD.md").write_text(record_markdown(manifest), encoding="utf-8")
    failures = [
        o["id"]
        for o in outputs
        if o["automated"].get("remediationStatus") != "REMEDIATED"
        or o["automated"]["independentComparator"]["result"] != "PASS"
    ]
    print(f"{len(outputs)} outputs prepared; automated failures: {failures or 'none'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
