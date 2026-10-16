"""Local deployment rendering and topology checks; no cloud calls or fake deployment claims."""

import json
import os
import subprocess
import sys
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
    assert "proxy_read_timeout 660s;" in nginx
    assert subprocess.run(command, cwd=root, env=env, capture_output=True).returncode != 0
    env["SERVER_NAME"] = "bad; injected"
    command[-1] = str(tmp_path / "invalid")
    assert subprocess.run(command, cwd=root, env=env, capture_output=True).returncode != 0
