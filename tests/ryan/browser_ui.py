"""Optional Chromium UI checks with a simulated HTTP target; not engine acceptance.

Run: python -m tests.ryan.browser_ui (requires Playwright and Chromium).
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2] / "app" / "ui"
HOSTILE = '<img src=x onerror="window.injected=1">'
RULES = ["ALT_TEXT", "WORKSHEET_NAME", "TABLE_HEADER", "LINK_TEXT", "MERGED_CELLS"]
VALUES = [
    {"title": None, "description": None},
    {"name": "Sheet1"},
    {"headerRow": 2},
    {"text": "click here"},
    None,
]


def check(browser, base, scenario):
    page = browser.new_page(accept_downloads=True)
    state = {"posts": [], "reads": [], "attempts": [], "latest": "a", "resume": False}
    findings = [
        {
            "findingId": str(i + 1) * 64,
            "ruleId": rule,
            "worksheet": "Sheet1",
            "target": "xl/worksheets/sheet1.xml#range=A2:B3",
            "message": HOSTILE,
            "classification": "MANUAL_REVIEW" if i == 4 else "REQUIRES_USER_INPUT",
            "recommendation": "Review this finding.",
        }
        for i, rule in enumerate(RULES)
    ]

    def route(r):
        request = r.request
        path = request.url.split("example.test", 1)[1]
        assert "token=" not in path
        path = path.removeprefix(base)
        if path == "/":
            r.fulfill(
                body=(ROOT / "templates/index.html").read_text().replace("{{ base }}", base),
                content_type="text/html",
            )
            return
        if path.startswith("/ui/") and path != "/ui/identity":
            filename = "app.js" if path.endswith("app.js") else "style.css"
            r.fulfill(
                path=ROOT / "static" / filename,
                content_type="text/javascript" if filename.endswith("js") else "text/css",
            )
            return
        assert request.headers.get("authorization") == "Bearer synthetic-user"
        assert "x-test-session" not in request.headers
        if request.method == "POST":
            state["posts"].append(path)
        else:
            state["reads"].append(path)
        if path == "/ui/identity":
            r.fulfill(json={"role": "reviewer" if scenario == "reviewer" else "user"})
        elif path in (
            "/api/v1/workbooks",
            "/api/v1/workbooks/w/versions",
            "/api/v1/workbooks/other/versions",
        ):
            if scenario == "upload-error":
                r.fulfill(status=422, json={"message": HOSTILE})
            else:
                r.fulfill(
                    status=202,
                    json={
                        "workbookId": "other" if "/other/" in path else "w",
                        "versionId": "v",
                        "auditId": "a",
                        "status": "PROCESSING",
                    },
                )
        elif path.endswith("/history"):
            r.fulfill(
                json={
                    "workbookId": "other" if "/other/" in path else "w",
                    "versions": [
                        {
                            "versionId": "v",
                            "sourceVersionId": None,
                            "status": "REMEDIATION_AVAILABLE",
                        }
                    ],
                    "audits": [
                        {
                            "versionId": "v",
                            "auditId": state["latest"],
                            "status": "REMEDIATION_AVAILABLE",
                            "rulesetVersion": "2.0.0",
                        }
                    ],
                }
            )
        elif path.endswith("/remediations"):
            state["attempts"].append((request.headers["idempotency-key"], request.post_data))
            if scenario == "lost-acceptance" and len(state["attempts"]) == 1:
                r.abort()
            elif scenario == "validation":
                r.fulfill(
                    status=422,
                    json={"code": "INVALID_CORRECTION", "message": "Choose a valid replacement."},
                )
            elif scenario == "conflict":
                state["latest"] = "new-audit"
                r.fulfill(
                    status=409, json={"code": "STALE_AUDIT", "message": "A newer audit exists."}
                )
            else:
                r.fulfill(
                    status=202,
                    json={"remediationId": "r", "sourceVersionId": "v", "status": "REMEDIATING"},
                )
        elif path == "/api/v1/remediations/r":
            if scenario == "remediation-failure":
                r.fulfill(
                    json={
                        "status": "PROCESSING_FAILED",
                        "error": {"message": "Preservation verification failed."},
                    }
                )
            else:
                r.fulfill(
                    json={
                        "status": "REMEDIATED",
                        "outputVersionId": "out-v",
                        "outputId": "out",
                        "auditId": "out-a",
                    }
                )
        elif path.endswith("/source") or path.endswith("/workbook") or path.endswith("/report"):
            r.fulfill(
                body=b"downloaded artifact",
                headers={"Content-Disposition": 'attachment; filename="artifact.xlsx"'},
                content_type="application/octet-stream",
            )
        elif path.endswith("/audits") and request.method == "POST":
            state["latest"] = "new-audit"
            r.fulfill(
                status=202, json={"auditId": "new-audit", "versionId": "v", "status": "PROCESSING"}
            )
        elif "/versions/" in path and "/audits/" not in path:
            r.fulfill(
                json={
                    "fileName": HOSTILE,
                    "sha256": "a" * 64,
                    "sourceVersionId": None,
                    "latestAuditId": "out-a" if "out-v" in path else state["latest"],
                }
            )
        elif path.endswith("/review"):
            r.fulfill(
                json={
                    "items": [
                        {
                            "findingId": f["findingId"],
                            "currentValue": VALUES[i],
                            "reason": "Explicit approval required.",
                            "allowedActions": []
                            if scenario == "reviewer"
                            else ["DECLINE", "MANUAL_REVIEW"]
                            if i == 4
                            else ["APPLY", "DECLINE", "MANUAL_REVIEW"],
                        }
                        for i, f in enumerate(findings)
                    ]
                }
            )
        elif "/audits/" in path:
            status = "REMEDIATION_AVAILABLE"
            if scenario == "audit-failure":
                status = "PROCESSING_FAILED"
            elif scenario == "poll-resume" and not state["resume"]:
                r.abort()
                return
            r.fulfill(
                json={
                    "status": status,
                    "rulesetVersion": "2.0.0",
                    "findings": []
                    if scenario in ("clean", "audit-failure") or "out-v" in path
                    else findings,
                    "limitations": [HOSTILE],
                    "error": {"message": "Audit failed safely."}
                    if scenario == "audit-failure"
                    else None,
                }
            )
        else:
            raise AssertionError(path)

    page.route("**/*", route)
    page.goto("http://example.test" + base + "/")
    page.get_by_label("User access token").fill("synthetic-user")
    page.get_by_role("button", name="Connect", exact=True).click()
    page.locator("#workspace").wait_for(state="visible")
    if scenario == "reviewer":
        assert page.locator("#upload").is_hidden()
        page.get_by_label("Existing workbook ID").fill("w")
        page.get_by_role("button", name="Open history", exact=True).click()
        page.get_by_role("button", name="Open audit a", exact=False).click()
    else:
        page.get_by_label("Excel workbook (.xlsx)").set_input_files(
            {"name": "test.xlsx", "mimeType": "application/octet-stream", "buffer": b"x"}
        )
        page.keyboard.press("Tab")
        page.get_by_role("button", name="Upload and check").focus()
        assert page.locator("#submit").evaluate("e => getComputedStyle(e).outlineStyle") != "none"
        page.keyboard.press("Enter")
    if scenario == "upload-error":
        page.wait_for_function("document.querySelector('#error').textContent.length > 0")
        assert HOSTILE in page.locator("#error").inner_text()
    else:
        if scenario == "poll-resume":
            page.locator("#retry").wait_for(state="visible")
            state["resume"] = True
            page.locator("#retry").click()
        page.locator("#results").wait_for(state="visible")
        page.wait_for_function(
            "document.querySelector('#status').textContent === 'Selected audit loaded.'"
        )
        assert HOSTILE in page.locator("#metadata").inner_text()
        assert page.locator("#results-heading").evaluate("e => e === document.activeElement")
        if scenario == "reviewer":
            assert page.locator("#reaudit").is_hidden()
            assert page.locator("#send-decisions").is_hidden()
            assert page.locator("#findings select").count() == 0
            assert state["posts"] == []
        elif scenario == "switch-workbook":
            page.locator("#decision-0").select_option("APPLY")
            page.get_by_label("Alternative text title").fill("Draft for the first workbook")
            page.get_by_label("Existing workbook ID").fill("other")
            page.get_by_role("button", name="Open history", exact=True).click()
            page.wait_for_function(
                "document.querySelector('#history-workbook').textContent.includes('other')"
            )
            assert page.locator("#results").is_hidden()
            assert page.locator("#output-panel").is_hidden()
            assert "other" in page.locator("#upload-destination").inner_text()
            page.locator("#upload-kind").select_option("version")
            page.locator("#submit").click()
            page.wait_for_function(
                "document.querySelector('#status').textContent === 'Selected audit loaded.'"
            )
            assert state["posts"].count("/api/v1/workbooks/other/versions") == 1
            assert "other" in page.locator("#metadata").inner_text()
            page.get_by_label("Existing workbook ID").fill("w")
            page.get_by_role("button", name="Open history", exact=True).click()
            page.get_by_role("button", name="Open audit a", exact=False).click()
            page.locator("#decision-0").wait_for(state="visible")
            assert page.locator("#decision-0").input_value() == "APPLY"
            assert (
                page.get_by_label("Alternative text title").input_value()
                == "Draft for the first workbook"
            )
        elif scenario == "navigation":
            for control in ("source-download", "report-download"):
                with page.expect_download() as saved:
                    page.locator("#" + control).click()
                assert Path(saved.value.path()).read_bytes() == b"downloaded artifact"
            page.locator("#reaudit").click()
            page.wait_for_function(
                "document.querySelector('#metadata').textContent.includes('new-audit')"
            )
            assert state["posts"].count("/api/v1/workbooks/w/versions/v/audits") == 1
            page.locator("#upload-kind").select_option("version")
            page.locator("#submit").click()
            page.wait_for_function(
                "document.querySelector('#status').textContent === 'Selected audit loaded.'"
            )
            assert state["posts"].count("/api/v1/workbooks/w/versions") == 1
        elif scenario not in ("clean", "audit-failure", "poll-resume"):
            assert page.locator("#decision-4 option[value=APPLY]").count() == 0
            assert page.locator("#findings select").evaluate_all(
                "els => els.every(e => e.value === '')"
            )
            page.locator("#decision-0").select_option("APPLY")
            page.get_by_label("Alternative text title").fill("My title")
            page.get_by_label("Alternative text description").fill(
                "My exact description\nwith a newline"
            )
            page.locator("#decision-1").select_option("DECLINE")
            if scenario == "all-families":
                page.locator("#decision-1").select_option("APPLY")
                page.get_by_label("New worksheet name").fill("Student records")
                page.locator("#decision-2").select_option("APPLY")
                assert page.get_by_label("Confirmed header row").input_value() == "2"
                assert (
                    page.get_by_label("Confirmed header row").get_attribute("readonly") is not None
                )
                page.locator("#decision-3").select_option("APPLY")
                page.get_by_label("Descriptive link text").fill("Student score details")
            page.locator("#send-decisions").click()
            if scenario == "lost-acceptance":
                page.locator("#retry").wait_for(state="visible")
                page.locator("#retry").click()
            if scenario == "validation":
                page.wait_for_function(
                    "document.querySelector('#decision-0-error').textContent.length > 0"
                )
                assert page.get_by_label("Alternative text title").input_value() == "My title"
                assert page.locator("#retry").is_hidden()
            elif scenario == "conflict":
                page.wait_for_function(
                    "document.querySelector('#selection-note').textContent.includes('Conflict')"
                )
                assert page.locator("#send-decisions").is_hidden()
                page.get_by_role("button", name="Open audit new-audit", exact=False).click()
                page.locator("#decision-0").wait_for(state="visible")
                assert page.locator("#decision-0").input_value() == ""
                assert page.get_by_label("Alternative text title").input_value() == ""
            elif scenario == "remediation-failure":
                page.wait_for_function(
                    "document.querySelector('#output-status').textContent.includes('failed')"
                )
                assert page.locator("#output-download").is_hidden()
            else:
                page.locator("#output-download").wait_for(state="visible")
                page.wait_for_function(
                    "document.querySelector('#metadata').textContent.includes('out-v')"
                )
                with page.expect_download() as saved:
                    page.locator("#output-download").click()
                assert Path(saved.value.path()).read_bytes() == b"downloaded artifact"
                assert len(state["attempts"]) == (2 if scenario == "lost-acceptance" else 1)
                assert all(a == state["attempts"][0] for a in state["attempts"])
            body = json.loads(state["attempts"][0][1])
            assert body["auditId"] == "a"
            assert len(body["decisions"]) == (4 if scenario == "all-families" else 2)
            assert body["decisions"][0]["value"] == {
                "title": "My title",
                "description": "My exact description\nwith a newline",
            }
            if scenario == "all-families":
                assert [d["value"] for d in body["decisions"]][1:] == [
                    {"name": "Student records"},
                    {"headerRow": 2},
                    {"text": "Student score details"},
                ]
            else:
                assert "value" not in body["decisions"][1]
        else:
            if scenario == "audit-failure":
                assert "Audit failed safely" in page.locator("#empty").inner_text()
            elif scenario == "clean":
                assert "No findings" in page.locator("#empty").inner_text()
    assert page.locator("img").count() == 0
    assert page.evaluate("window.injected || 0") == 0
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    print(json.dumps({"base": base, "scenario": scenario, "status": "pass"}))
    page.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable", help="Optional local Chromium/Chrome executable.")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser_executable)
        for base in ("", "/system"):
            for scenario in (
                "workflow",
                "all-families",
                "navigation",
                "switch-workbook",
                "lost-acceptance",
                "validation",
                "conflict",
                "reviewer",
                "clean",
                "audit-failure",
                "remediation-failure",
                "poll-resume",
                "upload-error",
            ):
                check(browser, base, scenario)
        browser.close()


if __name__ == "__main__":
    main()
