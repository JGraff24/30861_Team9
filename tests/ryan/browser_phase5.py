"""Phase 5 browser failure-state and axe checks against a labeled simulated target.

Optional tools: Playwright 1.58.0 / Chromium, axe-core 4.11.0. Pass --axe-script
and --evidence-dir to retain raw scans; these are not manual acceptance evidence.
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.ryan.browser_ui import HOSTILE, ROOT


def scan(page, axe_script, evidence, name):
    if axe_script is None:
        return
    page.evaluate(axe_script.read_text())  # DevTools injection preserves the product CSP.
    result = page.evaluate("""async () => {
        const result = await axe.run(document, {runOnly: {type: 'tag',
            values: ['wcag2a', 'wcag2aa', 'wcag21aa']}});
        return result;
    }""")
    # Retain computed foreground/background ratios for text that axe may flag
    # as needing review (notably textarea contents and open dialog text).
    result["computedContrast"] = page.evaluate("""() => {
      const rgb = value => (value.match(/[\\d.]+/g) || []).map(Number);
      const luminance = color => rgb(color).slice(0, 3).map(v => {
        v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
      }).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      return ['#error', '#status', '#delete-description', '#decision-0-description',
        '#save-schedule', '#confirm-delete'].flatMap(selector => {
          const element = document.querySelector(selector);
          if (!element || !element.getClientRects().length) return [];
          const style = getComputedStyle(element);
          let background = 'rgb(255, 255, 255)';
          for (let parent = element; parent; parent = parent.parentElement) {
            const value = getComputedStyle(parent).backgroundColor;
            const parts = rgb(value);
            if (parts.length === 3 || parts[3] === 1) { background = value; break; }
          }
          const colors = [luminance(style.color), luminance(background)].sort((a,b) => b-a);
          return [{selector, foreground: style.color, background,
            ratio: (colors[0] + .05) / (colors[1] + .05), minimum: 4.5}];
        });
    }""")
    assert all(sample["ratio"] >= sample["minimum"] for sample in result["computedContrast"])
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"{name}.json").write_text(json.dumps(result, indent=2))
    assert not result["violations"], [(v["id"], v["nodes"]) for v in result["violations"]]


def check(browser, base, scenario, axe_script, evidence):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = {
        "writes": [],
        "deletes": 0,
        "deleted": False,
        "schedule": {
            "workbookId": "w",
            "enabled": False,
            "intervalDays": None,
            "complianceDeadline": None,
            "versionId": "generated-v",
            "nextAuditAt": None,
            "reminderDays": [90, 30, 7],
        },
    }

    def route(r):
        req = r.request
        path = req.url.split("example.test", 1)[1].removeprefix(base)
        if path == "/":
            r.fulfill(
                body=(ROOT / "templates/index.html").read_text().replace("{{ base }}", base),
                content_type="text/html",
            )
        elif path in ("/ui/app.js", "/ui/style.css"):
            r.fulfill(path=ROOT / "static" / path.split("/")[-1])
        else:
            assert req.headers["authorization"] == "Bearer synthetic-user"
            assert "x-test-session" not in req.headers
            assert "/test/clock" not in path
            if req.method in ("PUT", "DELETE", "POST"):
                state["writes"].append((req.method, path, req.post_data))
            if path == "/ui/identity":
                r.fulfill(json={"role": "reviewer" if scenario == "reviewer" else "user"})
            elif req.method == "DELETE":
                state["deletes"] += 1
                if state["deletes"] == 1 and scenario in ("busy", "predelete", "partial"):
                    code = {
                        "busy": "ACTIVE_OPERATION",
                        "predelete": "TEST_DELETION_FAILURE",
                        "partial": "DELETION_FAILED",
                    }[scenario]
                    r.fulfill(
                        status=409 if scenario == "busy" else 500,
                        json={"code": code, "message": HOSTILE},
                    )
                elif state["deletes"] == 1 and scenario == "lost-delete":
                    state["deleted"] = True
                    r.abort()
                elif state["deleted"]:
                    r.fulfill(status=404, json={"code": "NOT_FOUND", "message": "Not found"})
                else:
                    state["deleted"] = True
                    r.fulfill(status=204)
            elif path.endswith("/history"):
                if state["deletes"] and scenario in ("partial", "lost-delete"):
                    r.fulfill(status=404, json={"code": "NOT_FOUND", "message": "Not found"})
                    return
                r.fulfill(json={"workbookId": "w", "versions": [], "audits": []})
            elif path.endswith("/schedule"):
                if req.method == "PUT":
                    body = req.post_data_json
                    state["schedule"].update(body)
                    state["schedule"]["nextAuditAt"] = (
                        "2027-01-31T00:00:00Z" if body["enabled"] else None
                    )
                    if scenario == "lost-save" and len(state["writes"]) == 1:
                        r.abort()
                        return
                r.fulfill(json=state["schedule"])
            elif path.endswith("/notifications"):
                r.fulfill(
                    json={
                        "notifications": []
                        if scenario == "empty"
                        else [
                            {
                                "scheduledFor": "2027-01-26T00:00:00Z",
                                "versionId": "prior-v",
                                "status": "FAILED",
                                "message": HOSTILE,
                            }
                        ]
                    }
                )
            else:
                raise AssertionError(path)

    page.route("**/*", route)
    page.goto("https://example.test" + base + "/")
    name = ("root" if not base else "system") + "-" + scenario
    scan(page, axe_script, evidence, name + "-connect")
    page.get_by_label("User access token").fill("synthetic-user")
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_label("Existing workbook ID").fill("w")
    page.get_by_role("button", name="Open history", exact=True).click()
    expect(page.locator("#lifecycle-data")).to_be_visible()
    expect(page.locator("#schedule-summary")).to_contain_text("generated-v")
    scan(page, axe_script, evidence, name + "-schedule")
    if scenario == "reviewer":
        expect(page.locator("#schedule-form")).to_be_hidden()
        expect(page.locator("#delete-controls")).to_be_hidden()
        assert not state["writes"]
    else:
        page.get_by_label("Periodic audits").select_option("true")
        page.get_by_label("Interval in days").fill("0")
        page.locator("#save-schedule").click()
        expect(page.locator("#error")).to_contain_text("whole-number")
        assert not state["writes"]
        scan(page, axe_script, evidence, name + "-invalid-interval")
        page.get_by_label("Interval in days").fill("30")
        page.get_by_label("Institution-supplied deadline").fill("2027-04-26")
        page.get_by_role("button", name="Save schedule", exact=True).click()
        if scenario == "lost-save":
            expect(page.locator("#error")).to_contain_text("unconfirmed")
            assert len(state["writes"]) == 1
            page.locator("#refresh-lifecycle").click()
        else:
            expect(page.locator("#status")).to_have_text("Schedule saved.")
        expect(page.locator("#schedule-summary")).to_contain_text("2027-01-31T00:00:00Z")
        assert state["schedule"]["complianceDeadline"] == "2027-04-26"
        page.get_by_label("Periodic audits").select_option("false")
        page.locator("#save-schedule").click()
        expect(page.locator("#status")).to_have_text("Schedule saved.")
        assert state["schedule"]["complianceDeadline"] == "2027-04-26"
        page.locator("#clear-deadline").click()
        page.locator("#save-schedule").click()
        expect(page.locator("#status")).to_have_text("Schedule saved.")
        assert state["schedule"]["complianceDeadline"] is None
        page.locator("#delete-workbook").click()
        expect(page.locator("#delete-description")).to_contain_text("workbook w")
        expect(page.locator("#cancel-delete")).to_be_focused()
        # Browser keyboard assertions supplement, never replace, human inspection.
        page.keyboard.press("Tab")
        expect(page.locator("#confirm-delete")).to_be_focused()
        scan(page, axe_script, evidence, name + "-confirmation")
        page.keyboard.press("Escape")
        expect(page.locator("#delete-dialog")).not_to_be_visible()
        assert state["deletes"] == 0
        page.locator("#delete-workbook").click()
        page.locator("#confirm-delete").click()
        if scenario in ("busy", "predelete", "partial", "lost-delete"):
            expect(page.locator("#error")).not_to_have_text("")
            assert state["deletes"] == 1
            if scenario in ("partial", "lost-delete"):
                expect(page.locator("#lifecycle-data")).to_be_hidden()
                expect(page.locator("#delete-workbook")).to_contain_text("Retry")
                # Reopening a hidden cleanup reservation still permits explicit recovery.
                page.get_by_label("Existing workbook ID").fill("w")
                page.get_by_role("button", name="Open history", exact=True).click()
                expect(page.locator("#delete-state")).to_contain_text("unconfirmed")
                expect(page.locator("#upload-kind option[value=version]")).to_be_disabled()
            else:
                expect(page.locator("#lifecycle-data")).to_be_visible()
            scan(page, axe_script, evidence, name + "-error")
            page.locator("#delete-workbook").click()
            page.locator("#confirm-delete").click()
        expect(page.locator("#lifecycle-panel")).to_be_hidden()
        expect(page.locator("#workbook-id")).to_be_focused()
        assert state["deleted"]
    assert not errors, errors
    assert page.locator("img").count() == 0
    assert page.evaluate("window.injected || 0") == 0
    page.close()
    print(json.dumps({"base": base, "scenario": scenario, "status": "pass", "target": "simulated"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axe-script", type=Path)
    parser.add_argument("--evidence-dir", type=Path, default=Path("/tmp/ryan-phase5-scans"))
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for base in ("", "/system"):
            for scenario in (
                "workflow",
                "empty",
                "reviewer",
                "lost-save",
                "busy",
                "predelete",
                "partial",
                "lost-delete",
            ):
                check(browser, base, scenario, args.axe_script, args.evidence_dir)
        browser.close()


if __name__ == "__main__":
    main()
