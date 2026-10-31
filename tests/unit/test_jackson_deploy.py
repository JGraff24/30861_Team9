"""Local deployment rendering and topology checks; no cloud calls or fake deployment claims."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_deployment_renderer(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "review"
    env = os.environ | {
        "AWS_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "S3_BUCKET": "review-only-bucket",
        "SERVER_NAME": "review.invalid",
        "MONTHLY_BUDGET_USD": "30",
    }
    command = [sys.executable, "-m", "scripts.prepare_deploy", "--output", str(output)]
    subprocess.run(command, cwd=root, env=env, check=True, capture_output=True)
    for name in ("iam-role-policy.json", "bucket-policy.json"):
        assert json.loads((output / name).read_text())["Version"] == "2012-10-17"
    nginx = (output / "nginx.conf").read_text()
    assert "server_name review.invalid;" in nginx
    assert "proxy_set_header Host $host;" in nginx
    assert "proxy_read_timeout 1900s;" in nginx
    assert subprocess.run(command, cwd=root, env=env, capture_output=True).returncode != 0
    env["SERVER_NAME"] = "bad; injected"
    command[-1] = str(tmp_path / "invalid")
    assert subprocess.run(command, cwd=root, env=env, capture_output=True).returncode != 0


def test_docker_copied_source_builds_an_installed_phase4_wheel(tmp_path: Path) -> None:
    """Exercise the exact Docker COPY inputs without requiring a Docker daemon."""
    root = Path(__file__).resolve().parents[2]
    stage = tmp_path / "docker-copied-source"
    stage.mkdir()
    for line in (root / "deploy/Dockerfile").read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        fields = shlex.split(line)
        if not fields or fields[0] != "COPY" or fields[1].startswith("--from="):
            continue
        sources, destination = fields[1:-1], fields[-1]
        for source in sources:
            origin = root / source
            target = stage / destination
            if origin.is_dir():
                shutil.copytree(
                    origin,
                    target,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                if len(sources) > 1 or destination.endswith("/"):
                    target /= origin.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, target)
    subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel"],
        cwd=stage,
        check=True,
        capture_output=True,
    )
    wheel = next((stage / "dist").glob("*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        for asset in (
            "app/ui/templates/index.html",
            "app/ui/static/app.js",
            "app/ui/static/style.css",
            "tester/fixtures/manifest-phase4.json",
            "tester/fixtures/expectations-phase4.json",
            "tester/phase4/phase4-wire-schemas.json",
        ):
            assert asset in archive.namelist()
        archive.extractall(installed)
    script = """import sys
sys.path.insert(0, sys.argv[1])
from tester.config import TesterSettings
from tester.suite.manifest import load_suite
from tester.suite.registry import names
from tester.phase4.contract import definitions
settings = TesterSettings(_env_file=None)
suite = load_suite(settings.fixture_manifest_path, names())
assert settings.fixture_manifest_path.name == 'manifest-phase4.json'
assert suite.manifest.phase == '4'
assert suite.manifest.ruleset_version == '2.0.0'
assert suite.manifest.limits.suite_seconds == 1800
assert len(suite.data) >= 26
assert definitions()
"""
    subprocess.run(
        [sys.executable, "-c", script, str(installed)],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        check=True,
        capture_output=True,
    )


def test_designated_production_target_overrides_only_release_capabilities() -> None:
    from app.config import SystemSettings

    source = (Path(__file__).parents[2] / "deploy/compose.production.yaml").read_text()
    assert 'RULESET_VERSION: "2.0.0"' in source
    assert 'ENABLE_TEST_INTERFACE: "true"' in source
    assert 'ENABLE_TEST_AUDIT_FAULTS: "true"' in source
    assert (
        "FIXTURE_MANIFEST_PATH: /service/.venv/lib/python3.12/site-packages/"
        "tester/fixtures/manifest-phase4.json" in source
    )
    assert 'TESTER_RUN_TIMEOUT_SECONDS: "1800"' in source
    assert "env_file: ${TEAM9_ENV_FILE:" in source
    assert "SYSTEM_TOKEN_HASHES_JSON:" not in source
    assert "TARGET_AUTH_MAP_JSON:" not in source
    assert SystemSettings.model_fields["enable_test_audit_faults"].default is False
