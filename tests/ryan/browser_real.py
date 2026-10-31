"""Optional local Chromium smoke against real API, engine, storage and worker processes.

Run with Python 3.12 and optional Playwright tooling. No target or credentials are
accepted from the network. Every run uses fresh temporary state and loopback
ports; this is separate browser evidence, never an instructor coverage numerator.
"""

import argparse
import hashlib
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI

from app.config import SystemSettings
from app.main import create_app
from tests.unit.phase4_builder import Link, Sheet, Workbook, picture_drawing

REPO = Path(__file__).resolve().parents[2]


def create_server_app() -> FastAPI:
    """Mount the production app as a proxy-prefix equivalent, retaining its lifespan."""
    settings = SystemSettings(_env_file=None)
    system = create_app(settings)
    if not settings.api_root_path:
        return system

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with system.router.lifespan_context(system):
            yield

    parent = FastAPI(lifespan=lifespan)
    parent.mount(settings.api_root_path, system)
    return parent


def source_workbook() -> bytes:
    return Workbook(
        [
            Sheet(
                "Sheet1",
                {
                    "A1": "Name",
                    "B1": "Score",
                    "A2": "Ada",
                    "B2": 9,
                    "D5": "click here",
                    "F1": "Review the merged title",
                },
                links=[Link("D5", url="https://example.edu/scores")],
                drawing=picture_drawing("3"),
                merges=["F1:G1"],
            )
        ]
    ).build()


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def local_system(prefix: str, service_python: str) -> Iterator[tuple[str, dict[str, str]]]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    target = f"http://127.0.0.1:{port}{prefix}"
    tokens = {role: secrets.token_urlsafe(32) for role in ("owner", "other", "reviewer")}
    principals = {
        hashlib.sha256(token.encode()).hexdigest(): {
            "subject": role,
            "role": "reviewer" if role == "reviewer" else "user",
        }
        for role, token in tokens.items()
    }
    with tempfile.TemporaryDirectory(prefix="team9-real-ui-") as temporary:
        state = Path(temporary)
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(REPO),
            "APP_ENV": "test",
            "METADATA_DB_PATH": str(state / "system.sqlite3"),
            "BLOB_ROOT": str(state / "blobs"),
            "BLOB_BACKEND": "filesystem",
            "SYSTEM_TOKEN_HASHES_JSON": json.dumps(principals),
            "REVIEWER_GRANTS_JSON": json.dumps({"reviewer": ["owner"]}),
            "API_ROOT_PATH": prefix,
            "RULESET_VERSION": "2.0.0",
            "ENABLE_TEST_INTERFACE": "false",
            "ENABLE_CODE_MEASUREMENT": "false",
        }
        processes: list[subprocess.Popen[bytes]] = []
        with (state / "process.log").open("wb") as log:
            try:
                processes.append(
                    subprocess.Popen(
                        [
                            service_python,
                            "-m",
                            "uvicorn",
                            "tests.ryan.browser_real:create_server_app",
                            "--factory",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(port),
                            "--no-access-log",
                        ],
                        cwd=state,
                        env=environment,
                        stdout=log,
                        stderr=log,
                    )
                )
                deadline = time.monotonic() + 30
                with httpx.Client(timeout=2, trust_env=False) as http:
                    while True:
                        try:
                            response = http.get(target + "/api/v1/health")
                            if response.status_code == 200:
                                assert response.json()["rulesetVersion"] == "2.0.0"
                                break
                        except httpx.TransportError:
                            pass
                        if processes[0].poll() is not None or time.monotonic() >= deadline:
                            raise AssertionError("Local production API failed to become healthy.")
                        time.sleep(0.1)
                processes.append(
                    subprocess.Popen(
                        [service_python, "-m", "app.jobs.worker"],
                        cwd=state,
                        env=environment,
                        stdout=log,
                        stderr=log,
                    )
                )
                yield target, tokens
            finally:
                for process in reversed(processes):
                    stop(process)


def metadata(page: Any) -> dict[str, str]:
    names = page.locator("#metadata dt").all_text_contents()
    values = page.locator("#metadata dd").all_text_contents()
    return dict(zip(names, values, strict=True))


def loaded(page: Any) -> None:
    from playwright.sync_api import expect

    expect(page.locator("#status")).to_have_text("Selected audit loaded.", timeout=85000)


def connect(page: Any, target: str, token: str) -> None:
    page.goto(target + "/")
    page.get_by_label("User access token").fill(token)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.locator("#workspace").wait_for(state="visible")


def download(page: Any, control: str) -> bytes:
    with page.expect_download() as saved:
        page.locator("#" + control).click()
    return Path(saved.value.path()).read_bytes()


def exercise(browser: Any, target: str, tokens: dict[str, str], prefix: str) -> None:
    from playwright.sync_api import expect

    page = browser.new_page(accept_downloads=True)
    page.set_default_timeout(30000)
    connect(page, target, tokens["owner"])
    source = source_workbook()
    page.get_by_label("Excel workbook (.xlsx)").set_input_files(
        {
            "name": "scores.xlsx",
            "mimeType": "application/octet-stream",
            "buffer": source,
        }
    )
    page.get_by_role("button", name="Upload and check").click()
    loaded(page)
    selected = metadata(page)
    assert selected["Ruleset"] == "2.0.0"
    assert selected["Status"] == "REMEDIATION_AVAILABLE"
    workbook_id, version_id, audit_id = (
        selected["Workbook ID"],
        selected["Version"],
        selected["Audit"],
    )
    source_path = f"/api/v1/workbooks/{workbook_id}/versions/{version_id}"
    source_audit = source_path + "/audits/" + audit_id
    assert download(page, "source-download") == source
    source_report = download(page, "report-download")
    auth = {"Authorization": "Bearer " + tokens["owner"]}
    with httpx.Client(base_url=target, headers=auth, timeout=30, trust_env=False) as http:
        audit = http.get(source_audit).json()
        assert [f["ruleId"] for f in audit["findings"]] == [
            "ALT_TEXT",
            "LINK_TEXT",
            "MERGED_CELLS",
            "TABLE_HEADER",
            "WORKSHEET_NAME",
        ]
        assert all(field.input_value() == "" for field in page.locator("#findings select").all())
        for index, finding in enumerate(audit["findings"]):
            action = "DECLINE" if finding["ruleId"] == "MERGED_CELLS" else "APPLY"
            page.locator(f"#decision-{index}").select_option(action)
            if action == "DECLINE":
                assert page.locator(f"#decision-{index} option[value=APPLY]").count() == 0
        page.get_by_label("Alternative text title").fill("Scores")
        page.get_by_label("Alternative text description").fill("Scores for each student")
        page.get_by_label("Descriptive link text").fill("Student score details")
        page.get_by_label("New worksheet name").fill("Student results")
        assert page.get_by_label("Confirmed header row").input_value() == "1"
        assert page.get_by_label("Confirmed header row").get_attribute("readonly") is not None
        attempts: list[tuple[str, str]] = []

        def lose_first_ack(route: Any) -> None:
            attempts.append((route.request.headers["idempotency-key"], route.request.post_data))
            response = route.fetch()
            assert response.status == 202
            if len(attempts) == 1:
                route.abort()
            else:
                route.fulfill(response=response)

        page.route("**/api/v1/workbooks/*/versions/*/remediations", lose_first_ack)
        page.get_by_role("button", name="Submit selected decisions").click()
        page.locator("#retry").wait_for(state="visible")
        page.locator("#retry:not([disabled])").click()
        page.locator("#output-download").wait_for(state="visible", timeout=210000)
        loaded(page)
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        submitted = json.loads(attempts[0][1])
        assert submitted["auditId"] == audit_id
        assert [d["value"] for d in submitted["decisions"] if d["action"] == "APPLY"] == [
            {"title": "Scores", "description": "Scores for each student"},
            {"text": "Student score details"},
            {"headerRow": 1},
            {"name": "Student results"},
        ]
        assert "value" not in submitted["decisions"][2]
        output = download(page, "output-download")
        output_selected = metadata(page)
        assert output_selected["Version"] != version_id
        assert output_selected["Source version"] == version_id
        assert output_selected["SHA-256"] == hashlib.sha256(output).hexdigest()
        output_report = json.loads(download(page, "report-download"))
        assert output_report["sourceVersionId"] == version_id
        assert len(output_report["approvedChanges"]) == 4
        assert [f["ruleId"] for f in output_report["findings"]] == ["MERGED_CELLS"]
        assert http.get(source_path + "/source").content == source
        assert http.get(source_audit + "/report").content == source_report
        history = http.get(f"/api/v1/workbooks/{workbook_id}/history").json()
        assert len(history["versions"]) == len(history["audits"]) == 2
        with zipfile.ZipFile(io.BytesIO(output)) as archive:
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            assert workbook.find("s:sheets/s:sheet", ns).attrib["name"] == "Student results"
            assert "xl/tables/table1.xml" in archive.namelist()
        page.unroute("**/api/v1/workbooks/*/versions/*/remediations", lose_first_ack)
        second = http.post("/api/v1/workbooks", files={"file": ("other.xlsx", source)})
        assert second.status_code == 202
        other_workbook = second.json()["workbookId"]
        page.get_by_label("Existing workbook ID").fill(other_workbook)
        page.get_by_role("button", name="Open history", exact=True).click()
        expect(page.locator("#history-workbook")).to_contain_text(other_workbook)
        assert page.locator("#results").is_hidden()
        assert page.locator("#output-panel").is_hidden()
        expect(page.locator("#upload-destination")).to_contain_text(other_workbook)
        page.locator("#upload-kind").select_option("version")
        page.get_by_role("button", name="Upload and check").click()
        expect(page.locator("#metadata dd").nth(1)).to_have_text(other_workbook, timeout=85000)
        loaded(page)
        second_selected = metadata(page)
        second_path = f"/api/v1/workbooks/{other_workbook}/versions/{second_selected['Version']}"
        assert second_selected["Source version"] == "Original upload"
        assert second_selected["Version"] != second.json()["versionId"]
        assert len(http.get(f"/api/v1/workbooks/{workbook_id}/history").json()["versions"]) == 2
        assert len(http.get(f"/api/v1/workbooks/{other_workbook}/history").json()["versions"]) == 2
        # A real competing audit invalidates the old form; drafts cannot migrate
        # merely because finding hashes are identical on unchanged source bytes.
        page.locator("#decision-0").select_option("APPLY")
        page.get_by_label("Alternative text title").fill("An unsent historical draft")
        competing = http.post(second_path + "/audits", json={})
        assert competing.status_code == 202
        new_audit_id = competing.json()["auditId"]
        deadline = time.monotonic() + 80
        while True:
            status = http.get(second_path + "/audits/" + new_audit_id).json()["status"]
            if status != "PROCESSING":
                assert status == "REMEDIATION_AVAILABLE"
                break
            assert time.monotonic() < deadline, "Competing audit did not complete."
            time.sleep(0.2)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/remediations")
            )
        ) as rejection:
            page.get_by_role("button", name="Submit selected decisions").click()
        assert rejection.value.status == 409
        expect(page.locator("#selection-note")).to_contain_text("Conflict")
        assert page.locator("#send-decisions").is_hidden()
        page.get_by_role("button", name="Open audit " + new_audit_id, exact=False).click()
        expect(page.locator("#metadata dd").nth(3)).to_have_text(new_audit_id)
        loaded(page)
        assert page.locator("#decision-0").input_value() == ""
        assert page.get_by_label("Alternative text title").input_value() == ""
    reviewer = browser.new_page(accept_downloads=True)
    connect(reviewer, target, tokens["reviewer"])
    assert reviewer.locator("#upload").is_hidden()
    reviewer.get_by_label("Existing workbook ID").fill(workbook_id)
    reviewer.get_by_role("button", name="Open history", exact=True).click()
    reviewer.get_by_role(
        "button", name="Open audit " + output_selected["Audit"], exact=False
    ).click()
    loaded(reviewer)
    assert reviewer.locator("#reaudit").is_hidden()
    assert reviewer.locator("#send-decisions").is_hidden()
    assert reviewer.locator("#findings select").count() == 0
    assert download(reviewer, "source-download") == output
    foreign = browser.new_page()
    connect(foreign, target, tokens["other"])
    foreign.get_by_label("Existing workbook ID").fill(workbook_id)
    foreign.get_by_role("button", name="Open history", exact=True).click()
    expect(foreign.locator("#error")).not_to_have_text("")
    assert foreign.locator("#results").is_hidden()
    assert foreign.locator("#history-panel").is_hidden()
    assert all(not origin["localStorage"] for origin in page.context.storage_state()["origins"])
    print(
        json.dumps(
            {
                "base": prefix,
                "status": "pass",
                "evidence": "real-api-engine-storage-worker-browser",
                "checks": [
                    "five-rule seeded review",
                    "four correction forms",
                    "manual-only decline",
                    "lost acceptance same key/body",
                    "one output",
                    "source/report immutable",
                    "output hash/provenance",
                    "reviewer readonly/download",
                    "foreign owner refused",
                    "cross-workbook version destination",
                    "stale audit explicit refresh",
                ],
            }
        ),
        flush=True,
    )
    for opened in (page, reviewer, foreign):
        opened.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", help="Optional local Chromium/Chrome executable.")
    parser.add_argument(
        "--service-python", default=sys.executable, help="Locked Python for API/worker."
    )
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser_executable)
        try:
            for prefix in ("", "/system"):
                with local_system(prefix, args.service_python) as (target, tokens):
                    exercise(browser, target, tokens, prefix)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
