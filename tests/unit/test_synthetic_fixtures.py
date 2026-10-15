"""Dylan: committed synthetic fixtures against hand-authored expectations.json."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.audit.engine import OoxmlWorkbookEngine
from app.contracts.errors import WorkbookValidationError
from app.contracts.models import ProcessingLimits

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "dylan"
MANIFEST: dict[str, Any] = json.loads((FIXTURES / "expectations.json").read_text("utf-8"))
ENGINE = OoxmlWorkbookEngine()


def independent_id(source: str, worksheet: str, rule_id: str, target: str) -> str:
    payload = [source, MANIFEST["rulesetVersion"], rule_id, worksheet, target]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("name", sorted(MANIFEST["fixtures"]))
def test_fixture_matches_expectations(name: str) -> None:
    expected = MANIFEST["fixtures"][name]
    data = (FIXTURES / name).read_bytes()
    source = hashlib.sha256(data).hexdigest()
    assert source == expected["sha256"], "fixture bytes changed; review expectations.json"
    if "rejection" in expected:
        with pytest.raises(WorkbookValidationError) as caught:
            ENGINE.validate(data, name, ProcessingLimits())
        assert caught.value.status_code == expected["rejection"]["statusCode"]
        assert caught.value.code == expected["rejection"]["code"]
        return
    validated = ENGINE.validate(data, name, ProcessingLimits())
    assert validated.worksheet_count == expected["validation"]["worksheetCount"]
    assert validated.nonempty_cell_count == expected["validation"]["nonemptyCellCount"]
    assert validated.worksheet_parts == expected["validation"]["worksheetParts"]
    evaluation = ENGINE.audit(data, validated, MANIFEST["rulesetVersion"])
    actual = [
        {
            "ruleId": f.ruleId,
            "category": f.category.value,
            "worksheet": f.worksheet,
            "target": f.target,
            "classification": f.classification.value,
        }
        for f in evaluation.findings
    ]
    authored = [{k: v for k, v in item.items() if k != "seed"} for item in expected["findings"]]
    assert actual == authored
    for finding in evaluation.findings:
        assert finding.findingId == independent_id(
            source, finding.worksheet, finding.ruleId, finding.target
        )
    assert evaluation.limitations == expected["limitations"]
    assert hashlib.sha256(data).hexdigest() == source
