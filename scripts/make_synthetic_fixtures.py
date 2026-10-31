"""Write (or --check) the labeled synthetic fixtures. Expectations stay hand-authored."""

import argparse
import hashlib
import json
from pathlib import Path

from tester.phase4.vectors import vector_fixtures
from tester.phase4.workbooks import phase4_fixtures
from tester.suite.workbooks import (
    CORRUPT_BYTES,
    clean_workbook,
    edge_workbooks,
    hostile_workbooks,
    pod_contract_workbooks,
    seeded_workbook,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "synthetic"
PHASE4_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "phase4" / "synthetic"
POLICY_VECTORS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "phase4"
    / "phase4-policy-vectors.json"
)


def phase4_files() -> dict[str, bytes]:
    """Team 9 Phase 4 fixtures plus rule-vector packs (palette from the frozen vectors)."""
    palette = json.loads(POLICY_VECTORS.read_text(encoding="utf-8"))["default_indexed_rgb"]
    return {**phase4_fixtures(), **vector_fixtures(palette)}


def fixtures() -> dict[str, bytes]:
    return {
        "clean.xlsx": clean_workbook(),
        "seeded_issues.xlsx": seeded_workbook(),
        "corrupt.xlsx": CORRUPT_BYTES,
        **edge_workbooks(),
        **hostile_workbooks(),
        **pod_contract_workbooks(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed bytes differ")
    args = parser.parse_args()
    for directory, files in ((FIXTURE_DIR, fixtures()), (PHASE4_DIR, phase4_files())):
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            path = directory / name
            if args.check:
                if not path.exists() or path.read_bytes() != data:
                    raise SystemExit(f"stale synthetic fixture: {path}")
            else:
                path.write_bytes(data)
            label = f"{directory.parent.name}/{name}" if directory == PHASE4_DIR else name
            print(f"{label}: {len(data)} bytes sha256={hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
