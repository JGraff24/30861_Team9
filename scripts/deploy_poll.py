"""Check public main CI and update the existing EC2 host. Python 3.9+, no AWS API calls."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def github(path: str) -> dict[str, Any]:
    request = Request(
        "https://api.github.com/" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "team9-deployment"},
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Unexpected GitHub response")
    return result


def eligible(repository: str, sha: str) -> bool:
    if not REPOSITORY.fullmatch(repository) or not SHA.fullmatch(sha):
        raise ValueError("Invalid repository or commit SHA")
    query = urlencode({"branch": "main", "event": "push", "head_sha": sha, "per_page": 100})
    runs = github(f"repos/{repository}/actions/workflows/ci.yml/runs?{query}")["workflow_runs"]
    matching = [
        run
        for run in runs
        if run["head_sha"] == sha
        and run["head_branch"] == "main"
        and run["event"] == "push"
        and run["head_repository"]["full_name"] == repository
    ]
    if not matching:
        return False
    # An earlier successful run must not hide a failed or unfinished rerun.
    latest = max(matching, key=lambda run: (run["id"], run["run_attempt"]))
    return bool(latest["status"] == "completed" and latest["conclusion"] == "success")


def private_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            os.chmod(staged, 0o600)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(staged, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            staged.unlink(missing_ok=True)


def deployed_sha(root: Path) -> str:
    # Read only the non-secret release field; do not source environment files as shell code.
    for line in (root / "config/compose.env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "DEPLOYMENT_SHA":
            sha = value.strip().strip("'\"")
            if SHA.fullmatch(sha):
                return sha
    raise ValueError("A full DEPLOYMENT_SHA is required in compose.env")


def execute(candidate: Path, sha: str, repository: str) -> int:
    with subprocess.Popen([sys.executable, str(candidate), sha, repository]) as process:
        try:
            return process.wait(timeout=2400)
        except subprocess.TimeoutExpired:
            # Allow the host's SIGTERM handler time to restore the previous release.
            process.terminate()
            try:
                process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return 124


def finish_success(root: Path, sha: str) -> None:
    state = root / "config/deployment-state.json"
    if state.exists() and json.loads(state.read_text()).get("sha") == sha:
        installed = root / "automation/deploy_poll.py"
        updated = root / "releases" / sha / "scripts/deploy_poll.py"
        if updated.is_file():
            private_write(installed, updated.read_bytes())
        failed = root / "config/deploy-failed.json"
        if failed.exists() and json.loads(failed.read_text()).get("sha") == sha:
            failed.unlink(missing_ok=True)


def poll(root: Path, repository: str, retry: bool = False) -> None:
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("Invalid repository")
    publication = root / "config/publication-state.json"
    if publication.exists():
        if not retry:
            print("Publication is unfinished; inspect logs, then retry explicitly.", flush=True)
            return
        record = json.loads(publication.read_text())
        published_sha = record.get("sha", "")
        if not isinstance(published_sha, str) or not SHA.fullmatch(published_sha):
            raise ValueError("Invalid unfinished publication SHA")
        directory = root / "releases" / published_sha
        source = directory / "scripts/deploy_host.py"
        if record.get("directory") != str(directory) or not source.is_file():
            raise RuntimeError("The vetted published-release recovery script is unavailable")
        # This image and local script were already admitted by exact-SHA CI. Only
        # recover that running release; a newer main is separately gated next tick.
        returncode = execute(source, published_sha, repository)
        if returncode == 75:
            return
        if returncode != 0:
            private_write(
                root / "config/deploy-failed.json",
                json.dumps({"sha": published_sha, "returncode": returncode}).encode(),
            )
            raise RuntimeError("Published release recovery failed; inspect deployment logs")
        finish_success(root, published_sha)
        return
    result = subprocess.run(
        ["git", "ls-remote", "https://github.com/" + repository + ".git", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    fields = result.stdout.split()
    if not fields or not SHA.fullmatch(fields[0]):
        raise RuntimeError("Could not resolve the main branch")
    sha = fields[0]
    failed = root / "config/deploy-failed.json"
    failed_current = failed.exists() and json.loads(failed.read_text()).get("sha") == sha
    if failed_current and not retry:
        print(
            "Deployment failed or publication is unfinished; inspect logs, then retry explicitly.",
            flush=True,
        )
        return
    if sha == deployed_sha(root) and not failed_current:
        print("Production is current: " + sha, flush=True)
        return
    if not eligible(repository, sha):
        print("Waiting for successful push CI on main " + sha, flush=True)
        return
    request = Request(
        "https://raw.githubusercontent.com/" + repository + "/" + sha + "/scripts/deploy_host.py",
        headers={"User-Agent": "team9-deployment"},
    )
    with urlopen(request, timeout=60) as response:
        source = response.read(1024 * 1024 + 1)
    if len(source) > 1024 * 1024:
        raise ValueError("Deployment script exceeds the size limit")
    candidate = root / "config" / ("deploy-" + sha + ".py")
    private_write(candidate, source)
    print("Deploying tested main " + sha, flush=True)
    try:
        returncode = execute(candidate, sha, repository)
        if returncode == 75:
            print("Deployment deferred; the next timer tick will retry.", flush=True)
            return
        if returncode != 0:
            private_write(failed, json.dumps({"sha": sha, "returncode": returncode}).encode())
            raise RuntimeError(
                "Deployment failed; inspect team9-autodeploy journal before retrying"
            )
        finish_success(root, sha)
    finally:
        candidate.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the deployment host")
    os.umask(0o077)
    root = Path("/opt/team9")
    with (root / "config/autodeploy-poll.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another automatic deployment check is already running.")
            return
        poll(root, args.repository, args.retry)


if __name__ == "__main__":
    main()
