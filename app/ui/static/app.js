"use strict";
(() => {
  const el = id => document.getElementById(id);
  const base = document.body.dataset.base;
  const text = (id, value) => { el(id).textContent = value; };
  const node = (tag, value) => { const n = document.createElement(tag); n.textContent = value; return n; };
  const enc = encodeURIComponent;
  let credential = "", role = null, busy = false, selected = null, workbookId = null;
  let pending = null, output = null;
  const drafts = new Map();
  const key = s => `${s.workbookId}/${s.versionId}/${s.auditId}`;
  const versionPath = s => `/api/v1/workbooks/${enc(s.workbookId)}/versions/${enc(s.versionId)}`;
  const auditPath = s => `${versionPath(s)}/audits/${enc(s.auditId)}`;
  const owner = () => role === "user";
  function controls() {
    for (const button of document.querySelectorAll("button")) button.disabled = busy || !!pending;
    el("retry").hidden = !pending;
    el("retry").disabled = busy;
    el("token").disabled = busy || !!pending;
    el("upload").hidden = !owner();
    el("reaudit").hidden = !owner();
    el("submit").disabled = busy || !!pending || !owner();
    el("upload-kind").options[1].disabled = !workbookId;
    text("upload-destination", workbookId
      ? `Additional versions will be uploaded to workbook ${workbookId}, shown in history.`
      : "Open a workbook's history before uploading an additional version.");
    for (const field of el("review-form").querySelectorAll("input, select, textarea")) {
      field.disabled = busy || !!pending;
    }
  }
  class ApiError extends Error {
    constructor(response, body) {
      super(body.message || "The request could not be completed.");
      this.status = response.status; this.code = body.code; this.details = body.details || {};
    }
  }
  async function api(path, options = {}, binary = false) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 35000);
    try {
      const response = await fetch(base + path, {...options, signal: controller.signal, redirect: "error",
        cache: "no-store", headers: {Authorization: "Bearer " + credential, ...options.headers}});
      if (!response.ok) {
        let body;
        try { body = await response.json(); } catch { body = {message: `Request failed (${response.status}).`}; }
        throw new ApiError(response, body);
      }
      return binary ? {blob: await response.blob(), disposition: response.headers.get("Content-Disposition")} : await response.json();
    } finally { clearTimeout(timer); }
  }
  async function task(action) {
    if (busy) return;
    busy = true; controls(); text("error", "");
    try { await action(); }
    catch (error) {
      text("error", error.name === "AbortError"
        ? "Request timed out. This does not mean processing failed. Resume the pending operation if available; check history before repeating an upload or re-audit."
        : error.message);
      text("status", pending ? "Operation is pending. Resume to recover its result." : "Operation stopped.");
    } finally { busy = false; controls(); }
  }
  async function history(requestedId = workbookId) {
    const h = await api(`/api/v1/workbooks/${enc(requestedId)}/history`);
    if (selected && selected.workbookId !== h.workbookId) {
      // History chooses the new-version destination. Do not leave another
      // workbook's results or output beside that newly selected destination.
      selected = null; output = null;
      el("results").hidden = true; el("output-panel").hidden = true;
      text("selection-note", "Open an audit from this workbook's history to review it.");
    }
    workbookId = h.workbookId;
    text("history-workbook", `Workbook ${workbookId}. Oldest versions and audits first.`);
    el("history").replaceChildren();
    for (const version of h.versions) {
      const item = node("li", `Version ${version.versionId} — ${version.status}${version.sourceVersionId ? ` (derived from ${version.sourceVersionId})` : " (uploaded)"}`);
      for (const audit of h.audits.filter(a => a.versionId === version.versionId)) {
        const button = node("button", `Open audit ${audit.auditId} — ${audit.status} (${audit.rulesetVersion})`);
        button.type = "button";
        button.addEventListener("click", () => task(() => open({workbookId: h.workbookId, versionId: version.versionId, auditId: audit.auditId})));
        item.append(document.createElement("br"), button);
      }
      el("history").append(item);
    }
    el("history-panel").hidden = false;
  }
  function draftFor(s) {
    if (!drafts.has(key(s))) drafts.set(key(s), new Map());
    return drafts.get(key(s));
  }
  function render(version, audit, review) {
    const s = selected;
    el("metadata").replaceChildren();
    for (const [name, value] of Object.entries({Workbook: version.fileName, "Workbook ID": s.workbookId,
      Version: s.versionId, Audit: s.auditId, "Source version": version.sourceVersionId || "Original upload",
      "SHA-256": version.sha256, Ruleset: audit.rulesetVersion, Status: audit.status})) {
      el("metadata").append(node("dt", name), node("dd", value));
    }
    const isLatest = version.latestAuditId === s.auditId;
    text("selection-note", isLatest ? "Selected audit is the latest for this version." : `Historical audit. Latest audit: ${version.latestAuditId}. Saved drafts remain attached to this historical audit.`);
    el("findings").replaceChildren();
    const items = new Map((review?.items || []).map(item => [item.findingId, item]));
    const draft = draftFor(s);
    let canDecide = false;
    for (const [index, finding] of audit.findings.entries()) {
      const item = document.createElement("li");
      item.append(node("p", `${finding.ruleId} — ${finding.worksheet} — ${finding.target}`),
        node("p", `${finding.classification}: ${finding.message}`), node("p", `Recommended response: ${finding.recommendation}`));
      const context = items.get(finding.findingId);
      if (context) item.append(node("p", context.reason));
      if (context?.currentValue !== null && context?.currentValue !== undefined) {
        item.append(node("p", `Current value: ${JSON.stringify(context.currentValue)}`));
      }
      const actions = owner() && isLatest ? (context?.allowedActions || []) : [];
      if (actions.length) {
        canDecide = true;
        const stored = draft.get(finding.findingId) || {action: "", value: {...(context.currentValue || {})}};
        draft.set(finding.findingId, stored);
        const group = document.createElement("fieldset");
        group.append(node("legend", `Decision ${index + 1}: ${finding.ruleId}`));
        const id = `decision-${index}`;
        const label = node("label", "Action"); label.htmlFor = id;
        const select = document.createElement("select"); select.id = id;
        select.append(new Option("Unselected — leave unresolved", ""));
        for (const action of actions) select.append(new Option(action.replaceAll("_", " "), action));
        select.value = actions.includes(stored.action) ? stored.action : "";
        stored.action = select.value;
        const fields = document.createElement("div");
        fields.hidden = stored.action !== "APPLY";
        const specs = {ALT_TEXT: [["title", "Alternative text title"], ["description", "Alternative text description"]],
          WORKSHEET_NAME: [["name", "New worksheet name"]], LINK_TEXT: [["text", "Descriptive link text"]],
          TABLE_HEADER: [["headerRow", "Confirmed header row"]]};
        if (actions.includes("APPLY")) {
          if (finding.ruleId === "WORKSHEET_NAME") fields.append(node("p", "Renaming updates supported formulas and references throughout the workbook."));
          if (finding.ruleId === "TABLE_HEADER") fields.append(node("p", `Confirm the first row of this exact region: ${finding.target}. Existing header cell text is preserved.`));
          for (const [field, caption] of specs[finding.ruleId] || []) {
            const fieldId = `${id}-${field}`;
            const fieldLabel = node("label", caption); fieldLabel.htmlFor = fieldId;
            const input = document.createElement(field === "description" ? "textarea" : "input");
            input.id = fieldId; input.value = stored.value[field] ?? "";
            if (field === "headerRow") { input.type = "number"; input.readOnly = true; }
            input.addEventListener("input", () => { stored.value[field] = input.value; });
            input.setAttribute("aria-describedby", `${id}-error`);
            fields.append(fieldLabel, input);
          }
        }
        const error = node("p", ""); error.id = `${id}-error`; error.className = "field-error";
        error.dataset.findingId = finding.findingId; error.setAttribute("role", "alert");
        select.addEventListener("change", () => { stored.action = select.value; fields.hidden = select.value !== "APPLY"; error.textContent = ""; });
        group.append(label, select, fields, error); item.append(group);
      }
      el("findings").append(item);
    }
    text("empty", audit.status === "PROCESSING_FAILED" ? `Audit failed: ${audit.error?.message || "Processing failed."}`
      : audit.findings.length ? "All listed findings remain unresolved until an output audit confirms the correction."
      : `No findings within the scope of ruleset ${audit.rulesetVersion}. Human review is still needed.`);
    el("limitations").replaceChildren(...audit.limitations.map(value => node("li", value)));
    el("send-decisions").hidden = !canDecide;
    el("results").hidden = false; el("results-heading").focus();
  }
  async function open(s) {
    let [version, audit] = await Promise.all([api(versionPath(s)), api(auditPath(s))]);
    if (audit.status === "PROCESSING") {
      pending = {kind: "audit", selection: s}; await resume(); return;
    }
    let review = null, reviewNotice = "";
    if (version.latestAuditId === s.auditId && audit.status !== "PROCESSING_FAILED" && audit.rulesetVersion === "2.0.0") {
      try { review = await api(auditPath(s) + "/review"); }
      catch (error) {
        if (error.status !== 409) throw error;
        version = await api(versionPath(s));
        reviewNotice = ` Review controls unavailable: ${error.message} Open the latest audit or request a re-audit.`;
      }
    }
    selected = s; workbookId = s.workbookId; el("workbook-id").value = workbookId;
    render(version, audit, review);
    text("selection-note", el("selection-note").textContent + reviewNotice);
    await history();
    text("status", "Selected audit loaded.");
  }
  async function poll(path, processing) {
    const deadline = Date.now() + 210000;
    while (Date.now() < deadline) {
      const result = await api(path);
      if (result.status !== processing) return result;
      text("status", processing === "PROCESSING" ? "Processing workbook…" : "Applying and verifying selected decisions…");
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    throw new Error("Waiting paused. Processing may still be running; resume to check again.");
  }
  async function resume() {
    const operation = pending;
    if (!operation) return;
    if (operation.kind === "audit") {
      await poll(auditPath(operation.selection), "PROCESSING");
      // Clear only after retrieving a terminal audit. A read failure may be resumed.
      await open(operation.selection); pending = null; return;
    }
    if (!operation.remediationId) {
      try {
        const accepted = await api(versionPath(operation.selection) + "/remediations", {
          method: "POST", body: operation.body,
          headers: {"Content-Type": "application/json", "Idempotency-Key": operation.idempotencyKey}});
        operation.remediationId = accepted.remediationId;
      } catch (error) {
        // A transport/5xx failure may hide acceptance. Keep the exact key and body.
        if (error.status && error.status < 500) {
          pending = null;
          for (const field of document.querySelectorAll(".field-error")) {
            const sent = JSON.parse(operation.body).decisions.some(d => d.findingId === field.dataset.findingId);
            if (sent) field.textContent = error.message;
          }
          if (error.status === 409) {
            // Refresh metadata without moving any old decision onto the newer audit.
            await open(operation.selection);
            text("selection-note", el("selection-note").textContent + " Conflict: refresh history and explicitly open the desired audit. Your previous draft is retained.");
          }
        }
        throw error;
      }
    }
    const result = await poll(`/api/v1/remediations/${enc(operation.remediationId)}`, "REMEDIATING");
    el("output-panel").hidden = false;
    if (result.status === "PROCESSING_FAILED") {
      pending = null;
      text("output-status", `Remediation ${operation.remediationId} failed. ${result.error?.message || "No output was published."}`);
      throw new Error(result.error?.message || "Remediation failed; original bytes are preserved.");
    }
    output = {workbookId: operation.selection.workbookId, versionId: result.outputVersionId,
      auditId: result.auditId, outputId: result.outputId};
    text("output-status", `Remediation ${operation.remediationId} completed. Separate output version: ${result.outputVersionId}. Review its remaining findings below.`);
    el("output-download").hidden = false; el("output-review").hidden = false;
    await open(output); pending = null;
  }
  async function download(path, fallback) {
    const result = await api(path, {}, true);
    let filename = fallback;
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(result.disposition || "");
    const ascii = /filename="([^"]+)"/i.exec(result.disposition || "");
    try { filename = encoded ? decodeURIComponent(encoded[1]) : ascii ? ascii[1] : fallback; } catch { /* use fallback */ }
    filename = filename.replace(/[\\/\x00-\x1f\x7f]/g, "_");
    const url = URL.createObjectURL(result.blob);
    const link = document.createElement("a"); link.href = url; link.download = filename;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    text("status", "Download prepared.");
  }
  el("connect").addEventListener("submit", event => { event.preventDefault(); task(async () => {
    credential = el("token").value; role = null; selected = null; workbookId = null; output = null; drafts.clear();
    for (const id of ["workspace", "results", "history-panel", "output-panel"]) el(id).hidden = true;
    text("identity", "");
    const identity = await api("/ui/identity"); role = identity.role;
    text("identity", owner() ? "Connected as an owner." : "Connected as a read-only reviewer. Open a granted workbook by ID.");
    el("workspace").hidden = false; text("status", "Connected.");
  }); });
  el("upload").addEventListener("submit", event => { event.preventDefault(); task(async () => {
    if (!owner() || pending) return;
    const data = new FormData(); data.append("file", el("file").files[0]);
    text("status", "Uploading workbook…");
    const path = el("upload-kind").value === "version" ? `/api/v1/workbooks/${enc(workbookId)}/versions` : "/api/v1/workbooks";
    const accepted = await api(path, {method: "POST", body: data});
    pending = {kind: "audit", selection: accepted}; await resume();
  }); });
  el("open-workbook").addEventListener("submit", event => { event.preventDefault(); task(async () => {
    await history(el("workbook-id").value);
  }); });
  el("refresh-history").addEventListener("click", () => task(history));
  el("reaudit").addEventListener("click", () => task(async () => {
    if (!owner() || !selected || pending) return;
    try {
      const accepted = await api(versionPath(selected) + "/audits", {method: "POST"});
      pending = {kind: "audit", selection: {...selected, auditId: accepted.auditId}}; await resume();
    } catch (error) { if (error.status === 409) await open(selected); throw error; }
  }));
  el("review-form").addEventListener("submit", event => { event.preventDefault(); task(async () => {
    if (!owner() || !selected || pending) return;
    const decisions = [];
    for (const [findingId, entry] of draftFor(selected)) {
      if (!entry.action) continue;
      const decision = {findingId, action: entry.action};
      if (entry.action === "APPLY") {
        decision.value = {...entry.value};
        if ("headerRow" in decision.value) decision.value.headerRow = Number(decision.value.headerRow);
      }
      decisions.push(decision);
    }
    if (!decisions.length) throw new Error("Select at least one decision before submitting.");
    // getRandomValues is also available on local HTTP review deployments.
    const idempotencyKey = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, "0")).join("");
    pending = {kind: "remediation", selection: {...selected}, idempotencyKey,
      body: JSON.stringify({auditId: selected.auditId, decisions})};
    el("output-panel").hidden = true; el("output-download").hidden = true; el("output-review").hidden = true; output = null;
    await resume();
  }); });
  el("source-download").addEventListener("click", () => task(() => download(versionPath(selected) + "/source", "workbook.xlsx")));
  el("report-download").addEventListener("click", () => task(() => download(auditPath(selected) + "/report", "audit-report.json")));
  el("output-download").addEventListener("click", () => task(() => download(`/api/v1/outputs/${enc(output.outputId)}/workbook`, "remediated.xlsx")));
  el("output-review").addEventListener("click", () => task(() => open(output)));
  el("retry").addEventListener("click", () => task(resume));
  controls();
})();
