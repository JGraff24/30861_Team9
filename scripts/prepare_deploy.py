"""Render reviewable deployment files locally; never contacts or provisions AWS."""

import argparse
import json
import os
import re
from pathlib import Path
from string import Template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inputs = {
        name: os.environ.get(name, "")
        for name in (
            "AWS_ACCOUNT_ID",
            "AWS_REGION",
            "S3_BUCKET",
            "SERVER_NAME",
            "MONTHLY_BUDGET_USD",
        )
    }
    if not re.fullmatch(r"[0-9]{12}", inputs["AWS_ACCOUNT_ID"]):
        parser.error("Supply a 12-digit AWS_ACCOUNT_ID")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", inputs["AWS_REGION"]):
        parser.error("Supply AWS_REGION")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", inputs["S3_BUCKET"]):
        parser.error("Supply a valid S3_BUCKET")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", inputs["SERVER_NAME"]):
        parser.error("Supply a DNS SERVER_NAME")
    try:
        budget = float(inputs["MONTHLY_BUDGET_USD"])
        if not 0 < budget < float("inf"):
            raise ValueError("invalid budget")
    except ValueError:
        parser.error("Supply a positive MONTHLY_BUDGET_USD")
    args.output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "deploy"
    for name in ("nginx.conf", "iam-role-policy.json", "bucket-policy.json"):
        rendered = Template((source / (name + ".template")).read_text()).safe_substitute(inputs)
        if name.endswith(".json"):
            json.loads(rendered)
        target = args.output / name
        with target.open("x") as handle:
            handle.write(rendered)
    print("Rendered local review files; no cloud actions performed.")


if __name__ == "__main__":
    main()
