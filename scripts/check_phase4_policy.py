"""Check the tester's independent Phase 4 policy oracle against the frozen 2.1.0 vectors.

Covers P4-LOCAL-PALETTE-VECTORS: identity, White_Space, generic names, ambiguous labels,
contrast, tint, indexed/theme constants, header components and the duplicate-ID targets.
Also checks that every formatting example in the pack validates against its named definition.
"""

import json
from pathlib import Path
from typing import Any

from tester.phase4 import contract, policy
from tester.suite.identity import finding_id

PACK = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "phase4"
VECTORS = PACK / "phase4-policy-vectors.json"
EXAMPLES = PACK / "phase4-examples.json"


def _codepoint(label: str) -> str:
    return chr(int(label.removeprefix("U+"), 16))


def check_policy(vectors: dict[str, Any]) -> list[str]:
    """Return one line per verified group; raise ValueError on the first disagreement."""
    lines = []
    if vectors.get("specification_version") != contract.SPECIFICATION_VERSION:
        raise ValueError("policy vectors are not specification 2.1.0")

    for vector in vectors["identity_vectors"]:
        encoded = json.dumps(vector["input"], ensure_ascii=False, separators=(",", ":"))
        if (
            encoded != vector["compact_utf8_text"]
            or finding_id(*vector["input"]) != vector["expected"]
        ):
            raise ValueError(f"identity vector mismatch for {vector['input'][3]!r}")
    lines.append(f"identity: {len(vectors['identity_vectors'])} vectors")

    frozen = {_codepoint(point) for point in vectors["white_space_codepoints"]}
    if frozen != policy.WHITE_SPACE or "​" in frozen:
        raise ValueError("White_Space code points differ from the frozen list")
    lines.append(f"white space: {len(frozen)} code points, U+200B excluded")

    for vector in vectors["generic_names"]:
        if policy.is_generic_sheet_name(vector["name"]) != vector["triggers"]:
            raise ValueError(f"generic-name vector mismatch for {vector['name']!r}")
    lines.append(f"generic names: {len(vectors['generic_names'])} vectors")

    for vector in vectors["ambiguous_labels"]:
        actual = policy.is_ambiguous_label(vector["label"], vector["destination"])
        if actual != vector["triggers"]:
            raise ValueError(f"ambiguous-label vector mismatch for {vector['label']!r}")
    lines.append(f"ambiguous labels: {len(vectors['ambiguous_labels'])} vectors")

    if tuple(vectors["theme_index_names"]) != policy.THEME_INDEX_NAMES:
        raise ValueError("theme index order differs")
    palette = vectors["default_indexed_rgb"]
    if len(palette) != 64 or any(policy.hex_rgb(policy.parse_rgb(c)) != c for c in palette):
        raise ValueError("fallback indexed palette must be 64 uppercase RGB entries (0-63)")
    lines.append("theme order and 64-entry indexed palette")

    for vector in vectors["contrast"]:
        font, fill = policy.parse_rgb(vector["font_rgb"]), policy.parse_rgb(vector["fill_rgb"])
        ratio = policy.contrast_ratio(font, fill)
        if (
            not abs(ratio - vector["ratio"]) <= 1e-12
            or policy.contrast_passes(ratio, large=False) != vector["passes_normal"]
            or policy.contrast_passes(ratio, large=True) != vector["passes_large"]
            or (policy.is_nonneutral(font) or policy.is_nonneutral(fill))
            != vector["has_nonneutral_color"]
        ):
            raise ValueError(f"contrast vector mismatch for {vector['font_rgb']}")
    lines.append(f"contrast: {len(vectors['contrast'])} vectors")

    for vector in vectors["tint"]:
        tinted = policy.hex_rgb(
            policy.apply_tint(policy.parse_rgb(vector["base_rgb"]), vector["tint"])
        )
        if tinted != vector["expected_rgb"]:
            raise ValueError(f"tint vector mismatch at {vector['tint']}: {tinted}")
    lines.append(f"tint: {len(vectors['tint'])} vectors")

    for vector in vectors["header_components"]:
        values = {policy.parse_cell(ref): value for ref, value in vector["cells"].items()}
        found = [
            (component, policy.candidate_classification(component, values))
            for component in policy.components(values)
        ]
        findings = [(f"#range={c.range}", kind) for c, kind in found if kind is not None]
        if "expected_table_findings" in vector:
            expected = [tuple(item) for item in vector["expected_table_findings"]]
        else:
            expected = [(vector["expected_target_suffix"], vector["expected_classification"])]
        if findings != expected:
            raise ValueError(f"header-component vector mismatch: {vector['description']}")
    lines.append(f"header components: {len(vectors['header_components'])} vectors")

    targets = vectors["duplicate_id_targets"]
    if (
        targets != sorted(targets)
        or len(set(targets)) != 2
        or not all(
            target.startswith("xl/drawings/drawing1.xml#xml=/wsDr[1]/twoCellAnchor[")
            and target.endswith("]/pic[1]")
            for target in targets
        )
    ):
        raise ValueError("duplicate-ID targets must be two distinct indexed pic paths")
    lines.append("duplicate-ID targets: 2 indexed paths")
    return lines


def check_examples(examples: dict[str, Any]) -> list[str]:
    for example in examples["examples"]:
        contract.validate(example["definition"], example["value"])
        if example["definition"] == "RemediationRequest":
            contract.validate("Identifier", example["headers"]["Idempotency-Key"])
    vector = examples["identity_vector"]
    if finding_id(*vector["input_array"]) != vector["expected_finding_id"]:
        raise ValueError("examples identity vector mismatch")
    return [f"examples: {len(examples['examples'])} validate; identity vector matches"]


def main() -> None:
    lines = check_policy(json.loads(VECTORS.read_text(encoding="utf-8")))
    lines += check_examples(json.loads(EXAMPLES.read_text(encoding="utf-8")))
    lines.append(f"wire schemas: {len(contract.definitions())} definitions")
    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
