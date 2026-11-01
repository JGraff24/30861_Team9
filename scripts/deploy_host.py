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
import stat
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
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
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
        self.publication = root / "config" / "publication-state.json"
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
        command_timeout = 900
        if args and args[0] == "stop" and "--timeout" in args:
            grace = int(args[args.index("--timeout") + 1])
            command_timeout = max(command_timeout, grace + 30)
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
            timeout=command_timeout,
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

    def active_work(self, data: Path) -> int:
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
            # Read-only opens cannot create a fresh database at an incorrect path.
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as database:
                busy += database.execute(query).fetchone()[0]
                if path.name == "system.sqlite3":
                    # Retained Phase 3 databases do not have these Phase 4 tables.
                    tables = {
                        row[0]
                        for row in database.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "remediation_jobs" in tables:
                        busy += database.execute(
                            "SELECT count(*) FROM remediation_jobs "
                            "WHERE state IN ('QUEUED','RUNNING')"
                        ).fetchone()[0]
                    if "upload_staging" in tables:
                        busy += database.execute(
                            "SELECT count(*) FROM upload_staging "
                            "WHERE julianday(deadline)>julianday('now') "
                            "OR julianday(deadline) IS NULL"
                        ).fetchone()[0]
        return busy

    def wait_idle(self, data: Path) -> None:
        # Phase 4 suites run up to 1,800 s. This first drain leaves ingress live;
        # the stopped-writer check below closes the request-acceptance race.
        deadline = time.monotonic() + 1900
        while self.active_work(data):
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
        metadata = {}
        for name, path in (
            ("system.sqlite3", data / "system/system.sqlite3"),
            ("tester.sqlite3", data / "tester/tester.sqlite3"),
        ):
            info = path.stat()
            metadata[name] = {
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": stat.S_IMODE(info.st_mode),
            }
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
        private_write(destination / "database-metadata.json", json.dumps(metadata))
        return destination

    def restore_databases(self, data: Path, backup: Path) -> None:
        """Only call while ingress and all writers remain stopped before publication."""
        metadata = json.loads((backup / "database-metadata.json").read_text())
        for name, path in (
            ("system.sqlite3", data / "system/system.sqlite3"),
            ("tester.sqlite3", data / "tester/tester.sqlite3"),
        ):
            info = metadata[name]
            staged = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
                    staged = Path(target.name)
                    with (backup / name).open("rb") as source:
                        shutil.copyfileobj(source, target)
                    target.flush()
                    os.fchown(target.fileno(), info["uid"], info["gid"])
                    os.fchmod(target.fileno(), info["mode"])
                    os.fsync(target.fileno())
                # Every container writer is stopped. Never let a migrated WAL be
                # replayed against the restored Phase 3 database's pages.
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(path) + suffix).unlink(missing_ok=True)
                os.replace(staged, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)

    def verify_images(self, directory: Path, image: str, names: set[str]) -> None:
        services = self.compose(
            directory, self.config, "ps", "--status", "running", "--services", capture=True
        )
        if not names <= set(services.split()):
            raise RuntimeError("Required containers are not running")
        for name in sorted(names - {"proxy"}):
            container = self.compose(directory, self.config, "ps", "-q", name, capture=True)
            actual = self.run(
                "docker", "inspect", "--format", "{{.Config.Image}}", container, capture=True
            )
            if actual != image:
                raise RuntimeError("Container is running the wrong release image")

    def wait_private_healthy(self, directory: Path, image: str) -> None:
        system_probe = """import json
from urllib.request import urlopen
from app.config import SystemSettings
settings = SystemSettings(_env_file=None)
if (settings.ruleset_version != '2.0.0' or not settings.enable_test_interface
        or not settings.enable_test_audit_faults):
    raise RuntimeError('Phase 4 system release configuration is incomplete')
with urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5) as response:
    if json.load(response) != {'status': 'AVAILABLE', 'apiVersion': '1.0',
                               'rulesetVersion': '2.0.0'}:
        raise RuntimeError('Private system health is incompatible')
"""
        tester_probe = """import json
from urllib.error import HTTPError
from urllib.request import urlopen
from tester.config import TesterSettings
from tester.suite.manifest import load_suite
from tester.suite.registry import names
settings = TesterSettings(_env_file=None)
suite = load_suite(settings.fixture_manifest_path, names())
if (suite.manifest.phase != '4' or suite.manifest.manifest_version != '2.1.1'
        or suite.manifest.ruleset_version != '2.0.0'
        or suite.manifest.specification_version != '2.1.0'
        or suite.manifest.limits is None or suite.manifest.limits.suite_seconds != 1800
        or suite.manifest.limits.run_read_timeout_seconds < 1830):
    raise RuntimeError('Private tester suite is incompatible')
with urlopen('http://127.0.0.1:8001/openapi.json', timeout=5) as response:
    operation = json.load(response)['paths']['/test/report']['get']
parameter = next(p for p in operation['parameters'] if p['name'] == 'session_id')
if parameter['required'] is not True or parameter['schema'].get('type') != 'string':
    raise RuntimeError('Private tester schema is incompatible')
try:
    with urlopen('http://127.0.0.1:8001/test/report', timeout=5):
        raise RuntimeError('Private tester accepted unauthenticated access')
except HTTPError as error:
    if error.code != 401:
        raise RuntimeError('Private tester authentication is incompatible') from error
"""
        deadline = time.monotonic() + 120
        while True:
            try:
                self.verify_images(directory, image, {"system", "worker", "tester"})
                for service, probe in (("system", system_probe), ("tester", tester_probe)):
                    self.compose(
                        directory,
                        self.config,
                        "exec",
                        "-T",
                        service,
                        "python",
                        "-c",
                        probe,
                        capture=True,
                    )
                return
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Release failed private configuration/readiness checks"
                    ) from None
                time.sleep(5)

    def wait_healthy(
        self, directory: Path, image: str, hostname: str, expected_ruleset: str | None = None
    ) -> None:
        deadline = time.monotonic() + 120
        while True:
            try:
                self.verify_images(directory, image, {"system", "worker", "tester", "proxy"})
                base = "https://" + hostname
                with urlopen(base + "/system/api/v1/health", timeout=5) as response:
                    health = json.load(response)
                    if health.get("status") != "AVAILABLE":
                        raise RuntimeError("System health is unavailable")
                    phase4 = expected_ruleset == "2.0.0" or image == "team9-phase3:" + self.sha
                    if phase4 and health != {
                        "status": "AVAILABLE",
                        "apiVersion": "1.0",
                        "rulesetVersion": "2.0.0",
                    }:
                        raise RuntimeError("Public health is not the Phase 4 ruleset")
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

    def update_poller(self, directory: Path) -> None:
        # Only install from the release whose public health has just passed. This
        # also upgrades an older bootstrap after direct recovery of its first
        # Phase 4 publication failure.
        source = directory / "scripts/deploy_poll.py"
        target = self.root / "automation/deploy_poll.py"
        if source.is_file() and target.parent.is_dir():
            private_write(target, source.read_text())

    def recover_published(self, settings: dict[str, str], record: dict[str, Any]) -> None:
        """Recover the existing pinned image even if a different main commit now exists."""
        published_sha = record.get("sha", "")
        if not isinstance(published_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", published_sha):
            raise RuntimeError("The unfinished publication record is invalid")
        directory = self.root / "releases" / published_sha
        if (
            record.get("directory") != str(directory)
            or record.get("image") != "team9-phase3:" + published_sha
            or settings.get("TEAM9_IMAGE") != record["image"]
            or settings.get("DEPLOYMENT_SHA") != published_sha
        ):
            raise RuntimeError("The unfinished publication configuration does not match its image")
        # Do not recreate running writers, migrate, back up or restore any data.
        self.compose(
            directory,
            self.config,
            "up",
            "-d",
            "--pull",
            "never",
            "--no-deps",
            "--no-recreate",
            "system",
            "worker",
            "tester",
        )
        self.wait_private_healthy(directory, record["image"])
        self.compose(directory, self.config, "up", "-d", "--pull", "never", "--no-deps", "proxy")
        self.wait_healthy(
            directory, record["image"], settings["SERVER_NAME"], expected_ruleset="2.0.0"
        )
        private_write(self.state, json.dumps(record, indent=2))
        self.publication.unlink(missing_ok=True)
        self.update_poller(directory)
        print("Published release recovered without rolling back accepted data.", flush=True)

    def update(self) -> None:
        settings = read_env(self.config)
        data = Path(settings["EBS_DATA_DIR"])
        if not data.is_absolute() or not os.path.ismount(data):
            raise RuntimeError("Persistent data volume must be mounted before deployment")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", settings["SERVER_NAME"]):
            raise ValueError("Invalid configured hostname")
        if self.publication.exists():
            self.recover_published(settings, json.loads(self.publication.read_text()))
            return
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
            if not self.is_current():
                print("Main advanced while waiting for active work; production is unchanged.")
                return
            backup = None
            private_started = False
            publication_attempted = False
            try:
                # Existing long tester responses may finish gracefully. Once this
                # returns no new public request can enter the old or new writers.
                self.compose(previous, self.config, "stop", "--timeout", "1900", "proxy")
                self.compose(
                    previous, self.config, "stop", "--timeout", "90", "system", "worker", "tester"
                )
                # An old API request may have still been validating during the first
                # idle read. Never migrate/process a newly accepted race privately.
                if self.active_work(data):
                    raise TimeoutError("Work arrived during quiescence; deferring release")
                backup = self.backup(data, original, previous)
                private_write(self.config, candidate.read_text())
                private_started = True  # Also covers a lost Compose acknowledgement.
                self.compose(
                    self.release,
                    self.config,
                    "up",
                    "-d",
                    "--pull",
                    "never",
                    "--force-recreate",
                    "--remove-orphans",
                    "--no-deps",
                    "system",
                    "worker",
                    "tester",
                )
                self.wait_private_healthy(self.release, "team9-phase3:" + self.sha)
                # This is a one-way boundary: even a lost proxy-start acknowledgement
                # may hide newly accepted user writes. Backups can no longer be restored.
                release_state = {
                    "sha": self.sha,
                    "image": "team9-phase3:" + self.sha,
                    "directory": str(self.release),
                    "backup": str(backup),
                    "previous_directory": str(previous),
                    "previous_image": settings["TEAM9_IMAGE"],
                }
                private_write(self.publication, json.dumps(release_state, indent=2))
                publication_attempted = True
                self.compose(
                    self.release,
                    self.config,
                    "up",
                    "-d",
                    "--pull",
                    "never",
                    "--force-recreate",
                    "--no-deps",
                    "proxy",
                )
                self.wait_healthy(self.release, "team9-phase3:" + self.sha, settings["SERVER_NAME"])
                private_write(self.state, json.dumps(release_state, indent=2))
                self.publication.unlink(missing_ok=True)
                self.update_poller(self.release)
            except BaseException:
                if publication_attempted:
                    print(
                        "Release publication/readiness failed after ingress may have opened. "
                        "Keeping the new backend, configuration and migrated databases; "
                        "no old-image or database rollback was attempted. Inspect proxy, "
                        "container logs and retained backups before recovery.",
                        flush=True,
                    )
                    raise
                print(
                    "Private activation failed; restoring the quiescent previous release.",
                    flush=True,
                )
                try:
                    if private_started:
                        self.compose(
                            self.release,
                            self.config,
                            "stop",
                            "--timeout",
                            "90",
                            "system",
                            "worker",
                            "tester",
                        )
                        assert backup is not None
                        self.restore_databases(data, backup)
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
                    self.publication.unlink(missing_ok=True)
                except BaseException:
                    print(
                        "Recovery could not safely complete; keep ingress closed and inspect "
                        "the retained database backups and deployment logs.",
                        flush=True,
                    )
                    raise
                print(
                    "Previous release recovered with all pre-publication records retained.",
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
