"""Canonical targets and finding identities (Team 9 contract profile 1.0.0).

findingId = lowercase SHA256 hex of the UTF-8 compact JSON array
[sourceSha256, rulesetVersion, ruleId, worksheetName, target], using ensure_ascii=False,
separators (",", ":"), no Unicode normalization, no BOM and no trailing newline.
"""

import hashlib
import json
from collections.abc import Sequence

from app.contracts.models import Finding


def finding_id(
    source_sha256: str, ruleset_version: str, rule_id: str, worksheet: str, target: str
) -> str:
    payload = [source_sha256, ruleset_version, rule_id, worksheet, target]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def format_xml_path(path: Sequence[tuple[str, int]]) -> str:
    """Local element names with one-based indices among same-named siblings, from the root.

    Example: /wsDr[1]/twoCellAnchor[2]/pic[1]/nvPicPr[1]/cNvPr[1]
    """
    return "".join(f"/{name}[{index}]" for name, index in path)


def object_target(part: str, object_id: str) -> str:
    return f"{part}#id={object_id}"


def worksheet_target(part: str, sheet_id: str) -> str:
    return f"{part}#sheetId={sheet_id}"


def xml_target(part: str, path: Sequence[tuple[str, int]]) -> str:
    return f"{part}#xml={format_xml_path(path)}"


def finding_sort_key(finding: Finding) -> tuple[str, str, str]:
    """Unicode code-point order of (ruleId, worksheet, target)."""
    return (finding.ruleId, finding.worksheet, finding.target)
