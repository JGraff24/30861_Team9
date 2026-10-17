"""Write (or --check) the labeled synthetic fixtures. Expectations stay hand-authored."""

import argparse
import hashlib
from pathlib import Path

from tester.suite.workbooks import (
    CORRUPT_BYTES,
    clean_workbook,
    edge_workbooks,
    hostile_workbooks,
    seeded_workbook,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "synthetic"


def fixtures() -> dict[str, bytes]:
    return {
        "clean.xlsx": clean_workbook(),
        "seeded_issues.xlsx": seeded_workbook(),
        "corrupt.xlsx": CORRUPT_BYTES,
        **edge_workbooks(),
        **hostile_workbooks(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed bytes differ")
    args = parser.parse_args()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in fixtures().items():
        path = FIXTURE_DIR / name
        if args.check:
            if not path.exists() or path.read_bytes() != data:
                raise SystemExit(f"stale synthetic fixture: {path}")
        else:
            path.write_bytes(data)
        print(f"{name}: {len(data)} bytes sha256={hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
