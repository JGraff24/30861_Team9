"""Optional real Chromium checks: run with Playwright installed outside runtime deps."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2] / "app" / "ui"
HOSTILE = '<img src=x onerror="window.injected=1">'


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for base in ("", "/system"):
            for scenario in ("clean", "findings", "failure", "timeout", "transport", "upload"):
                page = browser.new_page()
                state = {"posts": 0, "gets": 0, "resume": False}

                def route(request, incoming=None, *, base=base, scenario=scenario, state=state):
                    path = request.request.url.split("example.test", 1)[1]
                    if path == base + "/":
                        body = (
                            (ROOT / "templates/index.html").read_text().replace("{{ base }}", base)
                        )
                        request.fulfill(body=body, content_type="text/html")
                    elif path == base + "/ui/app.js":
                        request.fulfill(path=ROOT / "static/app.js", content_type="text/javascript")
                    elif path == base + "/ui/style.css":
                        request.fulfill(path=ROOT / "static/style.css", content_type="text/css")
                    elif path == base + "/api/v1/workbooks":
                        state["posts"] += 1
                        assert request.request.headers["authorization"] == "Bearer synthetic-user"
                        assert "x-test-session" not in request.request.headers
                        request.fulfill(
                            status=422 if scenario == "upload" else 202,
                            json={
                                "workbookId": "w",
                                "versionId": "v",
                                "auditId": "a",
                                "message": HOSTILE,
                            },
                        )
                    elif path.endswith("/audits/a"):
                        state["gets"] += 1
                        if scenario == "transport" and not state["resume"]:
                            request.abort()
                            return
                        status = "AUDIT_COMPLETE"
                        if scenario == "failure":
                            status = "PROCESSING_FAILED"
                        elif (scenario == "timeout" and not state["resume"]) or state["gets"] == 1:
                            status = "PROCESSING"
                        elif scenario == "findings":
                            status = "REVIEW_REQUIRED"
                        request.fulfill(
                            json={
                                "status": status,
                                "rulesetVersion": "1.0.0",
                                "error": {"message": HOSTILE},
                                "limitations": [HOSTILE],
                                "findings": [
                                    {
                                        "worksheet": HOSTILE,
                                        "target": HOSTILE,
                                        "classification": "MANUAL_REVIEW",
                                        "message": HOSTILE,
                                        "recommendation": HOSTILE,
                                    }
                                ]
                                if scenario == "findings"
                                else [],
                            }
                        )
                    elif path.endswith("/versions/v"):
                        request.fulfill(
                            json={"fileName": HOSTILE, "sha256": "a" * 64, "versionId": "v"}
                        )
                    else:
                        raise AssertionError(path)

                page.route("**/*", route)
                page.goto("http://example.test" + base + "/")
                page.get_by_label("User access token").fill("synthetic-user")
                page.get_by_label("Excel workbook (.xlsx)").set_input_files(
                    {"name": "hostile.xlsx", "mimeType": "application/octet-stream", "buffer": b"x"}
                )
                page.get_by_role("button", name="Upload and check").focus()
                assert (
                    page.locator("#submit").evaluate("e => getComputedStyle(e).outlineStyle")
                    != "none"
                )
                if scenario == "timeout":
                    page.clock.install()
                page.keyboard.press("Enter")
                if scenario == "timeout":
                    page.wait_for_function(
                        "document.querySelector('#status').textContent.includes('Processing')"
                    )
                    page.clock.fast_forward(76000)
                if scenario in ("timeout", "transport"):
                    page.locator("#retry").wait_for(state="visible")
                    state["resume"] = True
                    page.get_by_role("button", name="Resume checking this upload").click()
                if scenario in ("failure", "upload"):
                    page.wait_for_function(
                        "document.querySelector('#error').textContent.length > 0"
                    )
                    assert page.locator("#retry").is_hidden()
                else:
                    page.locator("#results").wait_for(state="visible")
                    assert page.locator("#results-heading").evaluate(
                        "e => e === document.activeElement"
                    )
                    assert HOSTILE in page.locator("#metadata").inner_text()
                assert state["posts"] == 1
                assert page.locator("img").count() == 0
                assert page.evaluate("window.injected || 0") == 0
                assert page.evaluate("localStorage.length + sessionStorage.length") == 0
                print(json.dumps({"base": base, "scenario": scenario, "status": "pass"}))
                page.close()
        browser.close()


if __name__ == "__main__":
    main()
