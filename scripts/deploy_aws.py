"""CI eligibility and bounded SSM deployment. Uses the runner's AWS CLI and OIDC session."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def github(path: str) -> dict[str, Any]:
    request = Request(
        "https://api.github.com/" + path,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "User-Agent": "team9-deployment",
        },
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Unexpected GitHub response")
    return result


def eligible(repository: str, sha: str) -> bool:
    if not REPOSITORY.fullmatch(repository) or not SHA.fullmatch(sha):
        raise ValueError("Invalid repository or commit SHA")
    branch = github(f"repos/{repository}/branches/main")
    if branch["commit"]["sha"] != sha:
        print("Skipping superseded commit; main has advanced.")
        return False
    query = urlencode({"branch": "main", "event": "push", "head_sha": sha, "per_page": 100})
    runs = github(f"repos/{repository}/actions/workflows/ci.yml/runs?{query}")["workflow_runs"]
    # Inspect the latest attempt, so an older successful attempt cannot hide a failed rerun.
    matching = [
        run
        for run in runs
        if run["head_sha"] == sha
        and run["head_branch"] == "main"
        and run["event"] == "push"
        and run["head_repository"]["full_name"] == repository
    ]
    if not matching:
        raise RuntimeError("No push CI run exists for this main commit")
    latest = max(matching, key=lambda run: (run["id"], run["run_attempt"]))
    if latest["status"] != "completed" or latest["conclusion"] != "success":
        raise RuntimeError("The latest push CI run for this commit has not passed")
    return True


def aws(region: str, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        ["aws", "--region", region, "--no-cli-pager", *arguments, "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("Unexpected AWS response")
    return payload


def deploy(region: str, instance: str, document: str, sha: str) -> None:
    if not SHA.fullmatch(sha) or not re.fullmatch(r"i-[0-9a-f]{8,17}", instance):
        raise ValueError("Invalid deployment SHA or instance ID")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,128}", document):
        raise ValueError("Invalid deployment document name")
    response = aws(
        region,
        "ssm",
        "send-command",
        "--instance-ids",
        instance,
        "--document-name",
        document,
        "--parameters",
        json.dumps({"ReleaseSha": [sha]}),
        "--timeout-seconds",
        "120",
        "--comment",
        "Team 9 tested main " + sha,
    )
    command_id = response["Command"]["CommandId"]
    print("SSM command:", command_id, flush=True)
    deadline = time.monotonic() + 2600
    while time.monotonic() < deadline:
        try:
            invocation = aws(
                region,
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance,
            )
        except subprocess.CalledProcessError as error:
            if "InvocationDoesNotExist" not in error.stderr:
                raise
            time.sleep(5)
            continue
        status = invocation["Status"]
        if status not in {"Pending", "InProgress", "Delayed", "Cancelling"}:
            print(invocation.get("StandardOutputContent", ""))
            print(invocation.get("StandardErrorContent", ""))
            if status != "Success" or invocation["ResponseCode"] != 0:
                raise RuntimeError(f"SSM deployment ended with status {status}")
            print("Deployment command completed successfully.")
            return
        time.sleep(10)
    raise TimeoutError(
        f"Timed out waiting for SSM command {command_id}; inspect it before retrying"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--sha", required=True)
    run = commands.add_parser("run")
    run.add_argument("--sha", required=True)
    run.add_argument("--region", required=True)
    run.add_argument("--instance-id", required=True)
    run.add_argument("--document-name", required=True)
    args = parser.parse_args()
    if args.command == "check":
        allowed = eligible(os.environ["GITHUB_REPOSITORY"], args.sha)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("deploy=" + str(allowed).lower() + "\n")
    else:
        deploy(args.region, args.instance_id, args.document_name, args.sha)


if __name__ == "__main__":
    main()
