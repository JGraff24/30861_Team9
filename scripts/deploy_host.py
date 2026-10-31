"""Deploy on the existing AL2023 host. Standard library only; supports its Python 3.9."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from string import Template
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        parts = shlex.split(value, comments=True)
        if len(parts) > 1:
            raise ValueError("Compose environment values must be quoted when containing spaces")
        values[key.strip()] = parts[0] if parts else ""
    return values


def release_env(original: str, sha: str) -> str:
    replacements = {"TEAM9_IMAGE": "team9-phase3:" + sha, "DEPLOYMENT_SHA": sha}
    lines = []
    found = set()
    for line in original.splitlines():
        key = line.partition("=")[0].strip()
        if key in replacements:
            line = key + "='" + replacements[key] + "'"
            found.add(key)
        lines.append(line)
    if found != set(replacements):
        raise ValueError("Compose environment must already define TEAM9_IMAGE and DEPLOYMENT_SHA")
    return "\n".join(lines) + "\n"


def private_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            os.chmod(staged, 0o600)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


class Deployment:
    def __init__(self, root: Path, repository: str, sha: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Expected a full commit SHA")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        self.root, self.repository, self.sha = root, repository, sha
        self.config = root / "config" / "compose.env"
        self.state = root / "config" / "deployment-state.json"
        self.release = root / "releases" / sha
        self.url = "https://github.com/" + repository + ".git"

    def run(self, *command: str, capture: bool = False, timeout: int = 900) -> str:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
        return result.stdout.strip() if capture else ""

    def compose(self, directory: Path, env: Path, *args: str, capture: bool = False) -> str:
        return self.run(
            "docker",
            "compose",
            "--project-name",
            "team9-production",
            "--env-file",
            str(env),
            "-f",
            str(directory / "deploy/compose.production.yaml"),
            *args,
            capture=capture,
        )

    def is_current(self) -> bool:
        head = self.run("git", "ls-remote", self.url, "refs/heads/main", capture=True, timeout=60)
        return bool(head.split()) and head.split()[0] == self.sha

    def prepare_release(self, settings: dict[str, str]) -> None:
        self.release.mkdir(parents=True, exist_ok=True)
        self.run("git", "init", "--quiet", str(self.release))
        self.run("git", "-C", str(self.release), "fetch", "--depth", "1", self.url, self.sha)
        self.run("git", "-C", str(self.release), "checkout", "--detach", "FETCH_HEAD")
        actual = self.run("git", "-C", str(self.release), "rev-parse", "HEAD", capture=True)
        if actual != self.sha:
            raise RuntimeError("Checkout does not match tested SHA")
        template = self.release / "deploy/nginx.conf.template"
        rendered = Template(template.read_text()).safe_substitute(
            SERVER_NAME=settings["SERVER_NAME"]
        )
        (self.release / "deploy/nginx.conf").write_text(rendered)
        self.run(
            "docker",
            "build",
            "--quiet",
            "-f",
            str(self.release / "deploy/Dockerfile"),
            "--target",
            "measurement",
            "--label",
            "org.team9.release=" + self.sha,
            "-t",
            "team9-phase3:" + self.sha,
            str(self.release),
        )

    def wait_idle(self, data: Path) -> None:
        # Phase 4 suites run up to 1,800 s; wait as long as the tester proxy allows.
        deadline = time.monotonic() + 1900
        while True:
            busy = 0
            for path, query in (
                (
                    data / "system/system.sqlite3",
                    "SELECT count(*) FROM jobs WHERE state IN ('QUEUED','RUNNING')",
                ),
                (
                    data / "tester/tester.sqlite3",
                    "SELECT count(*) FROM tester_sessions WHERE state IN ('PREPARING','RUNNING')",
                ),
            ):
                # mode=ro prevents a missing/wrong path from creating a fresh database.
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as database:
                    busy += database.execute(query).fetchone()[0]
            if not busy:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Active audit/tester work did not finish; leaving the old release running"
                )
            print("Waiting for active audits/tester sessions to finish...", flush=True)
            time.sleep(10)

    def backup(self, data: Path, original: str, previous: Path) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        destination = data / "deployment-backups" / (stamp + "-" + self.sha)
        destination.mkdir(mode=0o700, parents=True)
        private_write(destination / "compose.env", original)
        private_write(
            destination / "previous-release.json", json.dumps({"directory": str(previous)})
        )
        for name, path in (
            ("system.sqlite3", data / "system/system.sqlite3"),
            ("tester.sqlite3", data / "tester/tester.sqlite3"),
        ):
            deadline = time.monotonic() + 60

            def progress(
                status: int, remaining: int, total: int, backup_deadline: float = deadline
            ) -> None:
                if time.monotonic() >= backup_deadline:
                    raise TimeoutError("SQLite backup deadline exceeded")

            with (
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source,
                sqlite3.connect(destination / name) as target,
            ):
                source.backup(target, pages=128, progress=progress)
        return destination

    def wait_healthy(self, directory: Path, image: str, hostname: str) -> None:
        deadline = time.monotonic() + 120
        while True:
            try:
                services = self.compose(
                    directory, self.config, "ps", "--status", "running", "--services", capture=True
                )
                if not {"system", "worker", "tester", "proxy"} <= set(services.split()):
                    raise RuntimeError("Required containers are not running")
                for name in ("system", "worker", "tester"):
                    container = self.compose(directory, self.config, "ps", "-q", name, capture=True)
                    actual = self.run(
                        "docker",
                        "inspect",
                        "--format",
                        "{{.Config.Image}}",
                        container,
                        capture=True,
                    )
                    if actual != image:
                        raise RuntimeError("Container is running the wrong release image")
                base = "https://" + hostname
                with urlopen(base + "/system/api/v1/health", timeout=5) as response:
                    if json.load(response).get("status") != "AVAILABLE":
                        raise RuntimeError("System health is unavailable")
                with urlopen(base + "/tester/openapi.json", timeout=5) as response:
                    operation = json.load(response)["paths"]["/test/report"]["get"]
                parameter = next(p for p in operation["parameters"] if p["name"] == "session_id")
                if parameter["required"] is not True or parameter["schema"].get("type") != "string":
                    raise RuntimeError("Tester report schema is incompatible")
                try:
                    with urlopen(base + "/tester/test/report", timeout=5):
                        raise RuntimeError("Tester accepted an unauthenticated report request")
                except HTTPError as error:
                    if error.code != 401:
                        raise RuntimeError("Tester authentication health check failed") from error
                return
            except (
                OSError,
                ValueError,
                KeyError,
                StopIteration,
                RuntimeError,
                subprocess.SubprocessError,
            ):
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Release failed health, image, schema or authentication checks"
                    ) from None
                time.sleep(5)

    def update(self) -> None:
        settings = read_env(self.config)
        data = Path(settings["EBS_DATA_DIR"])
        if not data.is_absolute() or not os.path.ismount(data):
            raise RuntimeError("Persistent data volume must be mounted before deployment")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", settings["SERVER_NAME"]):
            raise ValueError("Invalid configured hostname")
        if shutil.disk_usage(self.root).free < 2 * 1024**3:
            raise RuntimeError(
                "Less than 2 GiB free on the host; clean old build images before deploying"
            )
        if not self.is_current():
            print("Skipping superseded commit; production is unchanged.")
            return
        previous = self.root / "repo"
        if self.state.exists():
            saved: dict[str, Any] = json.loads(self.state.read_text())
            previous = Path(saved["directory"])
        if not (previous / "deploy/compose.production.yaml").is_file():
            raise RuntimeError("Previous release Compose file is missing")
        self.run("docker", "image", "inspect", settings["TEAM9_IMAGE"], capture=True)
        self.prepare_release(settings)
        original = self.config.read_text()
        candidate = self.config.parent / ("candidate-" + self.sha + ".env")
        private_write(candidate, release_env(original, self.sha))
        try:
            self.compose(self.release, candidate, "config", "--quiet")
            self.compose(self.release, candidate, "pull", "proxy")
            if not self.is_current():
                print("Main advanced during build; leaving production unchanged.")
                return
            self.wait_idle(data)
            backup = self.backup(data, original, previous)
            if not self.is_current():
                print("Main advanced while waiting for active work; production is unchanged.")
                return
            try:
                # Stop ingress first, then stop writers. No volume deletion or database restore.
                self.compose(previous, self.config, "stop", "--timeout", "90", "proxy")
                self.compose(
                    previous, self.config, "stop", "--timeout", "90", "system", "worker", "tester"
                )
                private_write(self.config, candidate.read_text())
                self.compose(
                    self.release,
                    self.config,
                    "up",
                    "-d",
                    "--pull",
                    "never",
                    "--force-recreate",
                    "--remove-orphans",
                )
                self.wait_healthy(self.release, "team9-phase3:" + self.sha, settings["SERVER_NAME"])
                private_write(
                    self.state,
                    json.dumps(
                        {
                            "sha": self.sha,
                            "image": "team9-phase3:" + self.sha,
                            "directory": str(self.release),
                            "backup": str(backup),
                            "previous_directory": str(previous),
                            "previous_image": settings["TEAM9_IMAGE"],
                        },
                        indent=2,
                    ),
                )
            except BaseException:
                print(
                    "Deployment failed; restoring the previous image and Compose configuration.",
                    flush=True,
                )
                private_write(self.config, original)
                self.compose(
                    previous,
                    self.config,
                    "up",
                    "-d",
                    "--pull",
                    "never",
                    "--force-recreate",
                    "--remove-orphans",
                )
                self.wait_healthy(previous, settings["TEAM9_IMAGE"], settings["SERVER_NAME"])
                print(
                    "Previous release recovered. Database backups were retained, not restored.",
                    flush=True,
                )
                raise
            print(
                "DEPLOYED "
                + self.sha
                + "; HTTPS health, images, tester schema and authentication passed.",
                flush=True,
            )
        finally:
            candidate.unlink(missing_ok=True)


def interrupted(signum: int, frame: object) -> None:
    raise InterruptedError("Deployment interrupted by signal " + str(signum))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha")
    parser.add_argument("repository")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the deployment host")
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    with Path("/opt/team9/config/deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another deployment is active; deferring this attempt.", flush=True)
            raise SystemExit(75) from None
        try:
            Deployment(Path("/opt/team9"), args.repository, args.sha).update()
        except TimeoutError as error:
            print("Deployment deferred: " + str(error), flush=True)
            raise SystemExit(75) from error


if __name__ == "__main__":
    main()
