"use strict";
(() => {
  const el = id => document.getElementById(id);
  const base = document.body.dataset.base;
  let accepted = null;
  let busy = false;
  const text = (id, value) => { el(id).textContent = value; };
  async function api(path, options = {}, deadline = Date.now() + 10000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.max(1, Math.min(10000, deadline - Date.now())));
    try {
      const response = await fetch(base + path, {...options, signal: controller.signal,
        headers: {Authorization: "Bearer " + el("token").value}});
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || body.error?.message || "Request failed. Check your token and try again.");
      return body;
    } finally { clearTimeout(timer); }
  }
  function render(version, audit) {
    el("metadata").replaceChildren();
    for (const [name, value] of Object.entries({Workbook: version.fileName, "Source SHA-256": version.sha256,
      Version: version.versionId, Ruleset: audit.rulesetVersion, Status: audit.status})) {
      const dt = document.createElement("dt"); dt.textContent = name;
      const dd = document.createElement("dd"); dd.textContent = value;
      el("metadata").append(dt, dd);
    }
    el("findings").replaceChildren();
    for (const finding of audit.findings) {
      const item = document.createElement("li");
      item.textContent = `${finding.worksheet} — ${finding.target}: ${finding.classification}. ${finding.message} Recommended response: ${finding.recommendation}`;
      el("findings").append(item);
    }
    text("empty", audit.findings.length ? "" : "No findings from these two checks. Human review is still needed.");
    el("limitations").replaceChildren();
    for (const limitation of audit.limitations) {
      const item = document.createElement("li"); item.textContent = limitation;
      el("limitations").append(item);
    }
    el("results").hidden = false;
    el("results-heading").focus();
  }
  async function poll() {
    const deadline = Date.now() + 75000;
    const path = `/api/v1/workbooks/${encodeURIComponent(accepted.workbookId)}/versions/${encodeURIComponent(accepted.versionId)}`;
    while (Date.now() < deadline) {
      const audit = await api(path + "/audits/" + encodeURIComponent(accepted.auditId), {}, deadline);
      if (audit.status !== "PROCESSING") {
        if (audit.status === "PROCESSING_FAILED") {
          accepted = null;
          throw new Error(audit.error?.message || "Workbook processing failed.");
        }
        const version = await api(path, {}, deadline);
        render(version, audit); accepted = null; text("status", "Checks finished."); return;
      }
      text("status", "Processing workbook…");
      await new Promise(resolve => setTimeout(resolve, Math.min(1000, Math.max(0, deadline - Date.now()))));
    }
    throw new Error("Checking paused after 75 seconds. Resume checking to retrieve this upload’s result.");
  }
  async function run(upload) {
    if (busy) return;
    busy = true; el("submit").disabled = true; el("retry").hidden = true;
    text("error", "");
    try {
      if (upload) {
        accepted = null; el("results").hidden = true;
        text("status", "Uploading workbook…");
        const data = new FormData(); data.append("file", el("file").files[0]);
        // Upload has its own 35-second intake budget and is never automatically retried.
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 35000);
        try {
          const response = await fetch(base + "/api/v1/workbooks", {method: "POST", body: data,
            signal: controller.signal, headers: {Authorization: "Bearer " + el("token").value}});
          const body = await response.json();
          if (!response.ok) throw new Error(body.message || body.error?.message || "Upload rejected.");
          accepted = body;
        } finally { clearTimeout(timer); }
      }
      await poll();
    } catch (error) {
      text("status", "Checking stopped.");
      text("error", error.name === "AbortError" ? "Request timed out. An upload may have been accepted; it was not retried." : error.message);
      el("retry").hidden = !accepted;
      if (accepted) el("retry").focus();
    } finally { busy = false; el("submit").disabled = false; }
  }
  el("upload").addEventListener("submit", event => { event.preventDefault(); run(true); });
  el("retry").addEventListener("click", () => run(false));
})();
