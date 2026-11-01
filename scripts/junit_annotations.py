"""Turn failed tests in a pytest JUnit XML report into GitHub Actions error annotations.

CI runs this only after a failed pytest step, so each failing test shows on the run summary
(and through the checks API) without anyone opening the full job log.
"""

import sys
from pathlib import Path

from defusedxml import ElementTree  # type: ignore[import-untyped]

MAX_ANNOTATIONS = 10  # GitHub keeps at most 10 error annotations per step
MAX_DETAIL = 1500


def escape(text: str) -> str:
    """Workflow-command escaping for the message part of `::error::`."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    """Property values (such as `title=`) also escape the `:` and `,` separators."""
    return escape(text).replace(":", "%3A").replace(",", "%2C")


def annotations(report: Path) -> list[str]:
    root = ElementTree.fromstring(report.read_bytes())
    lines = []
    for case in root.iter("testcase"):
        for outcome in ("failure", "error"):
            problem = case.find(outcome)
            if problem is None:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (problem.text or problem.get("message") or "").strip()
            if len(detail) > MAX_DETAIL:
                detail = "..." + detail[-MAX_DETAIL:]
            title = escape_property(f"{outcome} in {name}")
            lines.append(f"::error title={title}::{escape(detail)}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m scripts.junit_annotations REPORT.xml", file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.exists():
        print(f"::error::No JUnit report at {escape(str(report))}; pytest may not have started.")
        return 0
    found = annotations(report)
    for line in found[:MAX_ANNOTATIONS]:
        print(line)
    if len(found) > MAX_ANNOTATIONS:
        print(f"::error::{len(found) - MAX_ANNOTATIONS} more failing tests; see the job log.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
