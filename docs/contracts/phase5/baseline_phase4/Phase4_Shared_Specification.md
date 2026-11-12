# Phase 4 Shared Implementation Specification

Excel Accessibility Hub  
Specification 2.1.0 • Ruleset 2.0.0 • Revised October 28 2026

## 1 Versions and adoption

Use specification version `2.1.0`, audit `rulesetVersion: "2.0.0"`, and Phase 4 suite `manifest_version: "2.1.0"`. These values are versioned separately. Revision 2.1.0 changes test setup behavior and document scope; the six-rule audit policy remains ruleset 2.0.0. Keep `/api/v1` paths and health `apiVersion: "1.0"`; that string identifies the business API generation, not the ruleset. Add no undeclared health fields. A schema or audit-policy change increments the relevant version; fixture-only changes increment the manifest version. Never relabel an old audit as ruleset 2.0.0.

The deliberate changes from earlier documents are: six-rule findings and completion statuses; typed remediation `value` objects instead of Phase 2’s underspecified string; the additional source, report, and review-context paths; mandatory remediation idempotency; and the explicit Phase 4 test catalogs, limits, and time budgets below. These choices are the implementation target, not claims about current code. Old string-valued remediation examples must not be implemented alongside this contract.

Before freezing the shared suite, each team records its adoption of this revision, actual base URLs, target revision, fixture hashes, and private access provisioning. Adoption records the specification version, ruleset, suite version, fixture pack hashes, and the common expected results. This document specifies shared observable behavior and does not prescribe either implementation’s codebase, staffing, or hosting stack.


The instructor testing interface takes precedence. Business requirements remain those agreed in Phase 2. The setup, single-use run, report, outcome/source, and coverage standards are unchanged; the optional test fault configuration below stays inside the existing system setup object. All requirements in this specification are mandatory unless explicitly identified as optional or an example. Historical audits and retained test reports keep their original ruleset and observations.

Public health, duplicate-object locations, and preservation comparisons are normative choices in this revision. Their inclusion does not establish that an earlier cross-test answer key was correct or that a reported defect is fixed. Shared adoption includes agreement on the exact repeated-ID fixture bytes, finding tuples, and XML paths. An accepted clean fixture must finish successfully with its reviewed expected results; PROCESSING_FAILED is not an acceptable clean-fixture result and is not a substitute for an intentional failure test.

## 2 Scope and completion

Phase 4 includes new-version uploads, manual re-audits, the remaining four rules, human review, four supported correction types, separate output versions, output re-audits, source and output downloads, downloadable JSON audit reports, history, review UI, persistence, and the associated tester and documentation. It preserves original bytes, existing authentication, authorization, intake limits, isolated sessions, and controlled errors.

Scheduling, deadline reminders, notifications, advancing a test clock, and ordinary workbook deletion remain Phase 5 operations 11 through 14. Return the normal project 404 for these unimplemented routes; do not advertise successful placeholders. Test-session cleanup remains available and is separate from ordinary deletion. New Phase 4 operations still require correct authorization, bounded execution, and controlled failures. Do not defer those protections to Phase 5.

An empty finding list means only that the six rules found no issues within their declared scope. Never label a workbook ADA compliant. Do not generate meaning with AI, infer an image’s intended description, fetch hyperlink destinations, execute workbook formulas, run macros, or modify an external live workbook.

Phase 4 is complete only when all four correction families work for their supported profiles, AC 03 through 11 have supporting evidence, intake regressions pass, and both test directions have retained reports and triaged discrepancies. A suite percentage alone does not establish acceptance. The Phase 2 Excel inspection and real-world precision commitments remain required.

## 3 Shared representation and request rules

Business JSON uses camelCase. Instructor testing JSON uses snake_case. JSON is UTF-8 with `application/json`; binary downloads use the media types in Section 5. Resource IDs are opaque strings of 1 to 64 ASCII characters matching `[A-Za-z0-9_-]+`. New resource IDs are fresh even when bytes or filenames repeat. Finding IDs and SHA256 values are exactly 64 lowercase hexadecimal characters. Dates are UTC RFC3339 ending in `Z`; retain fractional seconds if needed. Completion must not precede start. A filename is display text of 1 to 255 characters, contains no control characters, and never selects a storage path.

All declared response fields are present, including nullable fields. Use `[]` for empty arrays. Reject unknown request fields; do not silently coerce booleans to integers or numbers to strings. Responses contain only declared fields. Messages and recommendations are nonblank plain text of 1 to 500 Unicode code points. Decisions have the smaller limits in Section 8. All string lengths count Unicode code points, not UTF-8 bytes. Preserve user text exactly after validation; trim only when a rule explicitly requires it. XML escaping is serialization, not content modification. Preserve tabs/newlines in XML attributes with character references, and preserve literal text that resembles an OOXML escape sequence instead of interpreting it as another character. Inline strings must use xml:space="preserve" when needed. Reject characters that XML 1.0 cannot represent.

IDs, enum values, status codes, hashes, targets, array ordering, field types, and nullability are exact comparisons. Error prose and finding prose may differ between teams if they explain the same issue. Each target must return identical finding prose across repeat audits of identical bytes under one ruleset. No runtime timestamps, resource IDs, or random text appear inside finding messages or limitations.

Project errors always use `{code,message,requestId,details}` with nonblank strings, a valid request ID, and a JSON object for details. `details` is `{}` if no safe detail is useful. Do not return stack traces, tokens, host paths, or workbook contents. Add `X-Request-ID` to every project response. Error codes in Section 10 are normative for new Phase 4 paths. Preserve the Phase 3 rule that only the `.xlsm` code is compared exactly for legacy intake rejections; other legacy rejection codes must still explain the cause.

Publish an OpenAPI 3.1.1 catalog with the actual delivered paths, complete request/response definitions, and bearer authentication declarations. Public health has no bearer requirement.

Malformed JSON, undeclared fields, wrong identifier formats, missing required envelope/action/value fields, and invalid enum values return 400. Well-formed correction values with the wrong rule-specific shape or invalid content return 422. Authentication and session authorization occur before resource content access. Section 4 specifies lookup and ownership precedence.

## 4 Identities and isolation

Health is public, including when the tester sends a session header. An unknown session header does not make public health private; health ignores that header. Every other business operation requires a valid user or explicitly granted reviewer token. Missing or invalid tokens return 401. A test-admin token has test-interface powers only and returns 403 on protected business routes.

Owners may read and mutate their own workbooks. A reviewer with an explicit owner grant may read source files, versions, audits, outputs, reports, and history; a reviewer may not upload, re-audit, submit decisions, or generate output. This read-only grant preserves the Phase 3 access model and avoids an implied write grant. User B cannot access user A’s resources. Reviewer role without a grant is insufficient. UI controls reflect these permissions, and the server enforces them independently.

For protected routes, process: authenticate; validate the supplied session and membership; resolve all path IDs and their parent-child relationships within the requested namespace; authorize owner or reviewer; validate operation state; validate body semantics; perform work. Unknown or mismatched IDs and resources outside the namespace return 404. A correctly identified resource in the same namespace belonging to another user returns 403. A valid but wrong-role caller returns 403. No blob is read before these checks.

`X-Test-Session` is required on tester business calls except deliberate negative-authentication probes. Omitted header selects ordinary data; it never discovers session data. Unknown or cleaned session returns 400; unauthorized session membership returns 403; a known session not READY returns 409. Ownership still applies inside a shared test namespace. All resources derived from a version inherit its owner and namespace; callers cannot provide an owner or namespace in JSON.

## 5 HTTP operation catalog

All paths are relative to the registered system base URL. Keep a configured base prefix such as `/system` when constructing URLs. The proxy may strip that prefix internally. No redirects are required or accepted by the shared tester. Do not append `/api/v1` to the registered base itself.

| Operation | Method and path | Success |
|---|---|---|
| 1 Health | `GET /api/v1/health` | 200 HealthResponse |
| 2 Initial upload | `POST /api/v1/workbooks` | 202 UploadResponse |
| 3 New upload version | `POST /api/v1/workbooks/{workbookId}/versions` | 202 UploadResponse |
| 4 Version | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}` | 200 VersionResponse |
| 5 Audit | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}` | 200 AuditResponse |
| 6 Manual re-audit | `POST /api/v1/workbooks/{workbookId}/versions/{versionId}/audits` | 202 ReauditResponse |
| 7 Decisions | `POST /api/v1/workbooks/{workbookId}/versions/{versionId}/remediations` | 202 RemediationAccepted |
| 8 Remediation status | `GET /api/v1/remediations/{remediationId}` | 200 RemediationResponse |
| 9 Output workbook | `GET /api/v1/outputs/{outputId}/workbook` | 200 binary XLSX |
| 10 History | `GET /api/v1/workbooks/{workbookId}/history` | 200 HistoryResponse |
| S Source workbook | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/source` | 200 binary XLSX |
| V Review context | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/review` | 200 ReviewResponse |
| R Audit report | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/report` | 200 AuditReport download |

Source and report downloads and review context are explicit additions supporting AC 03, 06, 07, 08, 09, and 10. They do not renumber the Phase 2 business catalog. The same source path returns the immutable bytes of an uploaded or generated version; output download returns the identical bytes of the corresponding generated version.

### 5.1 Health and uploads

Health returns exactly `{"status":"AVAILABLE","apiVersion":"1.0","rulesetVersion":"2.0.0"}` when dependencies needed to serve and process work are available; otherwise return 503 ProjectError. Checking dependencies must not write a workbook or reset data.

Operations 2 and 3 accept multipart with exactly one binary field named `file` and no other parts. Both validate before acceptance. A rejected upload creates no accepted workbook, version, audit, or job. Operation 3 requires ownership of the workbook, uses the existing workbook ID, and creates fresh version/audit IDs even for identical bytes or filenames. Uploaded versions have `sourceVersionId: null`; they are new user-supplied inputs, not derived outputs. Preserve upload order in history using a transactional internal sequence.

UploadResponse has exactly `workbookId`, `versionId`, `auditId`, and `status: "PROCESSING"`. Return 202 only after immutable storage and durable metadata/job initialization succeed. A fast worker does not change this response to a terminal status. Save original filename and raw-byte hash; do not deduplicate resource identity by filename or hash.

### 5.2 Version and audit retrieval

VersionResponse fields remain `workbookId`, `versionId`, `fileName`, `sha256`, `createdAt`, `status`, `latestAuditId`, and `sourceVersionId`. `latestAuditId` is nullable and identifies the most recently accepted audit for that version. Generated versions point `sourceVersionId` to the exact parent version. They carry the hash of their own downloadable bytes. Reads verify stored integrity and return a controlled 500 on mismatch; never substitute another version’s content.

AuditResponse fields remain `auditId`, `workbookId`, `versionId`, `rulesetVersion`, `status`, `startedAt`, `completedAt`, `findings`, `limitations`, and `error`. During PROCESSING, completion/error are null and findings/limitations are empty. A failed audit has PROCESSING_FAILED, a completion timestamp, a ProjectError, and no partial findings. A successful completed audit has a timestamp and null error. Section 9 defines completion statuses. Terminal audit records never change because a user made decisions or another audit ran.

### 5.3 Manual re-audit

Operation 6 accepts an absent body, `{}`, or `{"reason":"MANUAL_REAUDIT"}`. Reject other reasons/fields with 400. It creates a fresh audit on the same immutable version, not a copied version, and updates `latestAuditId` atomically. Earlier audits remain retrievable. Return `auditId`, `versionId`, `rulesetVersion`, and `status: "PROCESSING"`. Use the server’s currently installed ruleset; clients cannot select an arbitrary ruleset.

Allow only one active audit or remediation using that version at a time. A competing operation returns 409 ACTIVE_OPERATION. A previously failed audit may be followed by a manual re-audit. A remediation request must reference the latest successful completed audit, not an older record that no longer describes the latest accepted audit state. Re-audits use the same raw bytes; identical bytes and ruleset produce identical findings, including IDs.

### 5.4 Downloads and history

Workbook downloads use `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `Content-Length`, `Content-Disposition: attachment`, and `Cache-Control: no-store`. Use a sanitized ASCII fallback filename and UTF-8 `filename*` for non-ASCII names. A generated name is the original basename plus `-remediated.xlsx`, shortened if needed to 255 code points. The filename does not identify a version. Repeated GETs return byte-identical content. Do not redirect to unauthenticated object URLs. Unknown outputs return 404; a known reserved but unpublished output returns 409 OUTPUT_NOT_READY. Successful responses never contain partial XLSX bytes. Pre-read and verify the bounded blob before sending headers.

HistoryResponse contains exactly `workbookId`, `versions`, and `audits`. Version entries contain `versionId`, `sourceVersionId`, `status`, and `createdAt`. Audit entries contain `auditId`, `versionId`, `rulesetVersion`, and `status`. Order versions by the internal creation sequence, audits by acceptance sequence, both oldest first. Never sort primarily by UUID or filename; timestamps may tie. Include failed audits, all uploaded versions, and committed generated versions. Do not include failed temporary candidates as successful versions. This history shape follows Phase 2; retrieve complete metadata through operation 4 or 5.

Phase 4 history is not paginated. Return a controlled error if the declared response-size ceiling is exceeded rather than silently dropping old entries. A normal test namespace is small; do not clear ordinary history to satisfy a test.

### 5.5 Review context

The added V path exposes current correction values so the UI does not guess them or parse XLSX in the browser. ReviewResponse contains exactly `workbookId`, `versionId`, `auditId`, and `items`. Each item has exactly `findingId`, `allowedActions`, `currentValue`, and `reason`, ordered like the audit findings. For the owner, a supported user-input finding has actions `["APPLY","DECLINE","MANUAL_REVIEW"]`; a manual-only finding has `["DECLINE","MANUAL_REVIEW"]`. A granted read-only reviewer receives an empty allowedActions array for every item. This response never grants permissions beyond Section 4.

For ALT_TEXT, currentValue is `{title,description}` with the exact existing strings or null for an absent attribute. For WORKSHEET_NAME it is `{name}` with the raw current name. For a supported LINK_TEXT correction it is `{text}` with the exact current label, possibly empty. For TABLE_HEADER it is `{headerRow}` with the absolute first row being proposed for confirmation. Other unsupported representations have currentValue null. These are read values, not validated replacement values; existing strings may exceed input limits and must not be silently truncated. Reason is nonblank text of at most 500 code points explaining the available action or restriction.

Create the owner correction context from the same immutable inventory and safety preflight used by the audit, and persist it with the completed audit. Project it into read-only actions for a reviewer. A current PROCESSING or failed audit returns 409 AUDIT_NOT_READY; an older audit after another audit was accepted returns 409 STALE_AUDIT. A legacy audit without a stored Phase 4 context returns 409 LEGACY_REVIEW_UNAVAILABLE; request a new audit. A completed clean audit returns items empty. All parent/namespace/owner checks still apply. Do not add current values to Finding or instructor SuiteReport.

## 6 Intake and common workbook interpretation

Retain the baseline limits: 25,000,000 file bytes, 50 worksheets, and 250,000 nonempty cells. Exactly at a limit is valid. Keep the file-byte ceiling separate from multipart overhead: permit up to 65,536 additional request bytes, eight headers per part, and 8,192 bytes per header; excess/malformed multipart structure returns 400 INVALID_REQUEST. Exceeding bytes returns 413; other invalid workbook conditions return 422. Chartsheets are audited for names and objects but do not count toward the worksheet limit. Hidden and very hidden worksheets count and are audited. No visible-sheet-only shortcut is allowed.

A nonempty cell for intake has a formula, a nonempty stored value, or nonempty inline-string content. Numeric zero and boolean false count; formatting-only cells do not. For rule evaluation, resolve shared strings and inline rich-text runs by concatenating text runs in document order, without phonetic annotations. A resolved whitespace-only string is not populated for table/merge/color analysis; a formula is populated even without cached text. Do not evaluate a formula or retrieve external data. Cached values are preserved but are not newly calculated.

Use Unicode White_Space for rule trimming: U+0009 through U+000D, U+0020, U+0085, U+00A0, U+1680, U+2000 through U+200A, U+2028, U+2029, U+202F, U+205F, and U+3000. U+200B is not whitespace under this policy. For case-insensitive comparisons explicitly requested below, use Unicode casefold unless the rule says ASCII. Do not normalize stored text or sheet names.

Reject .xls, CSV, missing extensions, links, and other unsupported types with 415. A named .xlsm returns 422 MACRO_ENABLED_WORKBOOK_UNSUPPORTED. A .xlsx containing VBA or macro parts also returns 422. Reject corrupt, encrypted, ZIP traversal, duplicate ZIP path, unsafe XML, DTD/entity, and actively protected workbook/worksheet inputs. A stored password with protection explicitly disabled is not active protection. MIME headers alone do not establish the file type.

Keep ZIP ceilings of 10,000 entries, 209,715,200 total expanded bytes, and 52,428,800 bytes per XML part; measure actual expanded bytes. Resolve package relationships internally, normalize safe package paths, and never fetch external relationships. A valid workbook may contain unsupported features: retain it for audit, disclose the affected locations, and use manual review rather than deleting those features. Malformed structural references needed to interpret a rule cause a controlled processing failure; do not declare uninspected data clean.

Numeric cell references are canonical uppercase A1 with no `$`, no sheet prefix, and valid Excel row/column bounds. Canonical ranges are `A1:D9`, always including both endpoints. For a stored row with omitted r, infer the next row after the previous stored row, beginning at 1; for a cell with omitted r, infer the next column in that row, beginning at A. Explicit row/cell references must be in bounds and consistent, with no duplicate address. Reject inconsistent/duplicate cell coordinates with 422 at validation when detected, or a controlled audit failure if discovered after acceptance. Sort rectangles and cells by numeric row, then numeric column when constructing a rule’s inventory; final findings still follow Section 7 sorting.

## 7 Audit rules and finding identity

Run all six rules against each accepted version. Rules are presence, structure, and deterministic screening checks; meaningful content and subjective structure require human judgment. No Phase 4 rule is classified AUTOMATICALLY_REMEDIABLE because all supported changes require user-provided meaning or confirmation. Keep that enum for future compatible rules, but do not use it to bypass consent.

| ruleId | category | Typical classification |
|---|---|---|
| ALT_TEXT | NON_TEXT_CONTENT | REQUIRES_USER_INPUT for safe supported objects; otherwise MANUAL_REVIEW |
| WORKSHEET_NAME | WORKSHEET_NAME | REQUIRES_USER_INPUT when rename profile is safe; otherwise MANUAL_REVIEW |
| TABLE_HEADER | TABLE_STRUCTURE | REQUIRES_USER_INPUT for a safe header candidate; otherwise MANUAL_REVIEW |
| MERGED_CELLS | MERGED_CELLS | MANUAL_REVIEW |
| LINK_TEXT | LINK_TEXT | REQUIRES_USER_INPUT for a safe simple cell hyperlink; otherwise MANUAL_REVIEW |
| COLOR_CONTRAST | COLOR_OR_CONTRAST | MANUAL_REVIEW |

Finding fields are exactly `findingId`, `ruleId`, `category`, `worksheet`, `target`, `classification`, `message`, and `recommendation`. Preserve raw worksheet spelling. Use `__WORKBOOK__` only for package-level objects with no owning sheet. Include a nonblank limitation whenever a representation cannot be fully assessed. Deduplicate identical limitations and sort their strings by Unicode code point. Do not collapse distinct object findings merely because their stored IDs repeat.

Compute `findingId = SHA256(UTF8(compact JSON([sourceSha256,rulesetVersion,ruleId,worksheet,target])))`. JSON uses `ensure_ascii=False`, separators `(',', ':')`, no BOM, no newline, and no Unicode normalization. Do not include audit/version IDs, classification, timestamps, messages, or input filenames. Sort final findings by `(ruleId, worksheet, target)` in Unicode code-point order. A shared identity vector is supplied in the companion examples. Cross-team comparison uses the independently calculated expected ID, not an ID copied from one target.

### 7.1 Canonical targets

| Subject | Target |
|---|---|
| Unique DrawingML or VML object ID | Actual package part plus `#id=<stored-id>` |
| Missing or duplicate object ID | Actual package part plus `#xml=<indexed-object-element-path>` |
| Worksheet name | Actual worksheet or chartsheet part plus `#sheetId=<stored-sheetId>` |
| Formal table header issue | Actual table part plus `#tableId=<stored-table-id>` |
| Unstructured table candidate | Actual worksheet part plus `#range=A1:D9` |
| Merged populated range | Actual worksheet part plus `#merge=A1:D1` |
| Simple hyperlink or static contrast cell | Actual worksheet part plus `#cell=A1` |
| Conditional color region | Actual worksheet part plus `#conditional=A1:D9` |
| Unsupported object or malformed link representation | Indexed XML element path in its actual part |

An indexed path starts at the document root and uses local element names with one-based indices among siblings of the same local name, for example `/wsDr[1]/twoCellAnchor[2]/pic[1]`. For duplicate cNvPr IDs, target the drawing object element such as `pic`, not the nested `cNvPr`. Use a fallback for every member of the duplicate-ID group, not just the second occurrence. Local-name paths are location labels; resolve edits through the already validated namespace-aware inventory, never by evaluating caller-supplied XPath. The repeated-ID fixture must state both exact paths. A whole unsupported package part uses `#xml=/`. No fabricated IDs, filesystem paths, or display-name targets are allowed.

For the repeated-ID layout containing two sibling twoCellAnchor elements, each with one pic using the same stored object ID, the two ALT_TEXT findings use these exact targets. Both have category NON_TEXT_CONTENT and classification MANUAL_REVIEW; worksheet is the actual owning worksheet name. They remain two distinct findings with distinct finding IDs. The fixture’s complete expected array also accounts for any other independently seeded rules.

```text
xl/drawings/drawing1.xml#xml=/wsDr[1]/twoCellAnchor[1]/pic[1]
xl/drawings/drawing1.xml#xml=/wsDr[1]/twoCellAnchor[2]/pic[1]
```

### 7.2 Alternative text

Inspect pictures, charts, shapes, connectors, text boxes, grouped children, and hidden-sheet objects using package XML. A nondecorative supported object missing either its cNvPr title or description after specified whitespace trimming produces one finding, even if both fields are missing. An object’s name, visible chart title, or text-box text does not substitute. Both fields present means no missing-alt finding; the rule does not judge their quality.

A group is a container; inspect children separately. Do not flag a group merely because it has no title/description. An explicit native decorative marker on a group or object yields one MANUAL_REVIEW finding to confirm that it carries no essential meaning. Decorative true suppresses a second missing-alt finding for that object. Only one native `http://schemas.microsoft.com/office/drawing/2017/decorative` marker with value `1`, `0`, `true`, or `false` is recognized. Ambiguous or multiple markers require manual review and a limitation.

Missing/duplicate nonvisual IDs, alternate content, VML, OLE, form controls, SmartArt, external object content, background images, IMAGE formulas, in-cell pictures, and rich-data representations retain the Phase 3 manual-review behavior. Inspect actual package parts; do not use a library object model that silently drops these. A missing/duplicate-ID object is MANUAL_REVIEW even if a missing-alt correction might otherwise look simple. Correcting these representations is outside the safe Phase 4 profile, but their preservation is required when an unrelated safe edit is possible.

### 7.3 Worksheet names

After Unicode White_Space trimming, match ASCII case-insensitive `Sheet` followed only by ASCII digits whose numeric value is greater than zero. `Sheet1`, `sheet2`, `Sheet01`, and ` Sheet3 ` trigger. `Sheet0`, `Sheet`, `Sheet١`, and `Expenses` do not. Inspect chartsheets and hidden sheets too. Produce one finding per matching sheet with its actual part and sheetId. Determine the correction classification using the rename safety profile in Section 8.4, not by assuming any XML name edit is safe.

### 7.4 Table headers

First inspect every formal SpreadsheetML table reachable from a worksheet’s table relationships. Resolve its rectangle, table-column metadata, and header-row count. An omitted headerRowCount means 1; 0 means no declared header. Count 1 with nonblank distinct plain-text header cells matching table-column names is structurally acceptable. The rule does not certify that those labels are meaningful. Flag one finding per table if the header is disabled, missing, blank, duplicated after trim and casefold, formula-based, merged, overlapping another table, or inconsistent with column metadata. A header count other than 0 or 1 is unsupported and manual. Totals rows are not header rows. Duplicate or missing table IDs use the indexed table-root XML target and require manual review.

Then find unstructured data outside formal table rectangles. Start from populated cells and compute four-neighbor connected components, ignoring empty cells and ignoring cells already covered by a formal table. A component is a candidate when its bounding rectangle is at least two rows by two columns. Do not join components across empty rows, columns, or diagonal contact. A dense rectangle has every coordinate populated. Use sparse sets and bounding-box area/count comparisons; do not iterate over an enormous empty used-range rectangle. A dense candidate with distinct nonblank plain-text first-row labels, at least one body row, and the safe conditions in Section 8.6 produces one REQUIRES_USER_INPUT finding: ask the author to confirm that first row is the header. Other candidates produce one MANUAL_REVIEW finding with a limitation explaining that logical structure could not be established safely. Target the bounding rectangle. A merged range or overlap prevents automatic header support.

A single cell, one-row title, and one-column list are not inferred tables under this profile. State that limitation once per audit if such populated components exist, without manufacturing a table defect for them. A clean fixture may have this disclosure and still have zero findings. Do not infer semantic headers from bold text, color, or the first nonempty row. A new table created after confirmation is a structural correction, not an invented label or a value rewrite.

### 7.5 Merged cells

For each declared merge rectangle spanning more than one cell, produce one MANUAL_REVIEW finding if it contains at least one populated cell as defined in Section 6. This explicitly includes populated title merges: the finding asks whether the merge impedes navigation, rather than declaring every title merge an accessibility violation. A completely empty or formatting-only merged rectangle does not trigger. Do not automatically unmerge, copy values, or rearrange a table. Multiple identical merge declarations are one target; conflicting/overlapping merges require a limitation and manual review. This is a potential-barrier screening rule chosen to avoid guessing which populated region the author intended.

### 7.6 Hyperlink text

Inspect worksheet hyperlink elements and cell formulas calling HYPERLINK, including `_xlfn.HYPERLINK`, ASCII case-insensitively. Do not treat ordinary plain URLs without a hyperlink or HYPERLINK formula as links. For an element-based single-cell link, visible label is the resolved cell text, not only the hyperlink `display` attribute. A missing/whitespace label, raw destination text after trimming, or a complete casefolded label in `{click here, here, link, more, read more, learn more, website, url}` triggers. Collapse internal Unicode White_Space to one ASCII space for this comparison and ignore one trailing ASCII `.`, `!`, or `?`. Apply the prefix check to the same trimmed, whitespace-collapsed, casefolded label. A label beginning with `http://`, `https://`, `www.`, or `mailto:` is a raw-address label even if the relationship differs. Descriptive labels such as `Annual accessibility report` do not trigger. This is a finite screening vocabulary, not a claim to understand all ambiguous language.

A HYPERLINK formula with two literal string arguments is screened using its second argument; a one-argument formula uses the first argument as label. Parse Excel escaped double quotes and commas outside strings; accept a leading `=` and optional `_xlfn.`. Computed arguments, semicolon/localized syntax, and unsupported formula forms yield MANUAL_REVIEW plus a limitation. Do not execute the formula. Formula-based hyperlinks are never modified in Phase 4, even when a literal label is detected. A range link spanning multiple cells or duplicate hyperlink declarations affecting one cell also produces one manual-review item per affected representation. Supported simple text-cell links use the cell target; unsupported representations use indexed XML targets. Drawing-object hyperlinks remain covered by an explicit manual-review limitation if present, not a silent all-clear.

### 7.7 Color and contrast

Check populated worksheet cells, including hidden cells, for static text contrast and possible color-only meaning. A formula cell without usable cached text still receives a color-only screen when it uses a nonneutral color; disclose that rendered formula text was not calculated. Resolve cell style from explicit cell `s`, otherwise custom row style, otherwise the covering column style, otherwise cellXf 0. Resolve referenced base style attributes and apply flags; an unsupported or ambiguous style inheritance path produces a manual-review finding and limitation for that populated cell. Never rely on the workstation’s Excel theme.

For supported cells with ordinary string, numeric, boolean, error, or formula-cached display content, resolve 6/8-digit RGB, workbook theme colors with tint, and indexed colors from the workbook palette or the standard indexed palette. In 8-digit ARGB, use the RGB channels for SpreadsheetML cell colors. Resolve theme scheme entries in `lt1,dk1,lt2,dk2,accent1..accent6,hlink,folHlink` index order; a system color uses lastClr. Apply tint to HSL lightness: for negative tint, L becomes L*(1+t); for positive tint, L becomes L*(1-t)+t. Convert back to sRGB, clamp, and round to nearest integer with half values rounded upward. Missing fill means white and missing font color means black under this screening profile; disclose this display assumption once if used. Default font size is 11 pt. Resolve default/theme fonts where available. Unsupported automatic/system colors without a concrete RGB fallback, patterned/gradient fills, rich text with mixed styling, table-style overlays, and affected conditional formatting require manual review rather than a guessed numeric contrast result.

Calculate sRGB relative luminance using the W3C formula: normalize channels to 0..1; linearize at threshold 0.04045 with division by 12.92 below or equal, otherwise ((c+0.055)/1.055)^2.4. L is 0.2126R+0.7152G+0.0722B. Ratio is (lighter L+0.05)/(darker L+0.05). Require at least 4.5 for ordinary text and 3.0 for text at least 18 pt, or at least 14 pt and bold. Compare the unrounded ratio; equality passes. Report rounded display values only. These are screening decisions informed by W3C contrast guidance, not an Excel rendering or legal-compliance certification.

A nonneutral effective font or solid fill means its RGB channels are not all equal. It triggers a color-meaning MANUAL_REVIEW finding even when contrast passes: ask whether equivalent text or structure carries the information. The frozen fallback indexed palette in `phase4-policy-vectors.json` defines indices 0 through 63; indices 64 and 65 require manual review unless an explicit concrete color is available. Theme/index/auto/rgb declarations with conflicting active color sources are ambiguous and manual. For a cell with both low contrast and possible color-only meaning, combine the explanations into one COLOR_CONTRAST finding at that cell. A low-contrast grayscale pair still triggers. Default black on white does not trigger. Do not infer color meaning or automatically recolor.

For conditional formatting, emit one MANUAL_REVIEW finding per distinct declared range if its rule references differential font/fill colors, a color scale, data bar, icon set, or an unrecognized visual extension. Normalize each space-separated sqref rectangle separately, deduplicate ranges, and use the conditional target. Do not evaluate the condition. Suppress static contrast/color findings within those affected ranges to avoid presenting an unreliable second numeric result. Other conditional formatting without visual color changes does not trigger this rule. For unsupported static cell rendering, emit one cell finding and a limitation; do not also emit a guessed ratio.

## 8 Human decisions and supported corrections

Operation 7 accepts exactly `{auditId,decisions}`. Decisions is a nonempty array of at most 1,000 objects. Each object contains `findingId`, `action`, and, only for APPLY, `value`. Allowed actions are APPLY, DECLINE, and MANUAL_REVIEW. Refer to a finding in the exact latest completed successful audit of the path version. Duplicate finding IDs return 400 DUPLICATE_DECISION. Unknown or cross-audit finding IDs return 404 FINDING_NOT_FOUND. Omitted findings remain unresolved; their omission is not consent.

DECLINE records that the author leaves the finding unresolved. MANUAL_REVIEW records that it requires review outside the supported correction process. Both must omit `value`; reject a supplied value with 400. Neither action edits a workbook or removes a finding from the audit. APPLY requires the exact typed value below. Do not accept positional arguments, stringified JSON, inferred defaults, or arbitrary XML paths. A user cannot change the target or classification in the request.

| Rule | APPLY value object | Limits |
|---|---|---|
| ALT_TEXT | `{title,description}` | Both strings nonblank; 1 to 200 and 1 to 800 code points respectively |
| WORKSHEET_NAME | `{name}` | Excel name validation below; at most 31 code points |
| LINK_TEXT | `{text}` | Nonblank string of 1 to 1,000 code points |
| TABLE_HEADER | `{headerRow}` | Integer absolute worksheet row equal to the candidate rectangle’s first row |

The compact UTF-8 JSON encoding of each value must be no larger than 8,192 bytes. This replaces Phase 2’s generic 1,000-character string cap with rule-specific limits that represent both required alt fields without inventing a title. All string values must be legal XML 1.0 text. Reject empty or whitespace-only values. Store hyperlink labels as explicit text even when they begin with a formula-like character, as specified below. Preserve leading/trailing whitespace in accepted alt/link text; a worksheet name must already equal its trimmed form.

### 8.1 Example decision request

This is a formatting example, not the expected findings for a supplied fixture. Use real finding IDs from the selected source audit and a fresh `Idempotency-Key` such as `review-request-001`.

```json
{
  "auditId": "audit-001",
  "decisions": [
    {
      "findingId": "3f00eb9e00b43f60e672a109297c8d594eb12cd6753217be172a34101b01e859",
      "action": "APPLY",
      "value": {
        "title": "Monthly expenses",
        "description": "Bar chart comparing monthly expenses from January through June."
      }
    },
    {
      "findingId": "1111111111111111111111111111111111111111111111111111111111111111",
      "action": "DECLINE"
    }
  ]
}
```

### 8.2 Request validation and batch atomicity

Validate the entire decision set and its interactions before accepting a job. APPLY to MANUAL_REVIEW, MERGED_CELLS, COLOR_CONTRAST, a formula link, a decorative object, or a target outside the safe correction profile returns 422 UNSUPPORTED_CORRECTION. It must not downgrade to a partial success. A value that would leave the same rule’s structural defect unresolved returns 422 INVALID_CORRECTION. A missing value returns 400 MISSING_VALUE. Wrong value shape/content returns 422 INVALID_CORRECTION. More than 1,000 decisions or duplicate decisions returns 400 INVALID_REQUEST or DUPLICATE_DECISION respectively.

If any decision fails, accept no remediation, store no decision batch, and create no successful output. An accepted job is also all-or-nothing: any edit, validation, preservation, or output-audit failure makes the remediation fail. Do not publish a workbook containing only the earlier approved edits. Multiple decisions targeting one XML part are allowed when they do not conflict. Apply alt text, hyperlink text, header changes, then worksheet renames; within each family use source finding order. All target resolution uses the immutable source inventory, including when a sheet is renamed in the same batch.

### 8.3 Alternative text correction

Only supported uniquely identified DrawingML pictures, charts, shapes, connectors, and text boxes with unambiguous nonvisual properties are eligible. Set the object’s cNvPr `title` and `descr` to exactly the two user values. Use review context to prepopulate both fields, leaving an absent attribute empty. The user approves the complete pair, including any already-present field. Do not generate or truncate either field, change the object’s name, or set/clear decorative flags. Preserve object IDs, positions, media, chart bodies, relationships, extLst, and grouped structure. Shared drawing parts that make ownership ambiguous require manual review. Apply that same ownership rule to any editable table or worksheet part referenced by multiple owning sheets.

### 8.4 Worksheet rename correction

Require a nonblank name of 1 to 31 Unicode code points, already trimmed, containing no `:`, `\\`, `/`, `?`, `*`, `[`, `]`, control characters, or leading/trailing apostrophe. Reject reserved `History` case-insensitively and generic names under Section 7.3. Names must be unique across worksheets and chartsheets after Unicode casefold. Treat case-only renames as unsupported in Phase 4. An unchanged name is invalid. A batch may rename multiple sheets, but final names must be unique and cannot swap or reuse another source sheet name within the same batch; reject with 422 CONFLICTING_CORRECTIONS.

Preserve sheetId, part path, order, visibility, and relationships. Rewrite references to that exact sheet in supported cell formulas, workbook/local defined names, chart series formulas, data-validation formulas, conditional-formatting formulas, and internal hyperlink locations. Parse Excel sheet-reference tokens, including quoted names, escaped apostrophes, absolute cell references, unions, and ranges. Do not globally replace text or modify string literals, similar sheet names, external workbook names, or ordinary cell text. New sheet names in rewritten references are always quoted with apostrophes and embedded apostrophes doubled.

A rename is eligible only when the workbook’s complete reference-bearing inventory is understood. Any 3D sheet reference, external workbook link, INDIRECT/EVALUATE reference construction, pivot/cache/slicer/query connection, embedded opaque workbook, unsupported formula extension, signed package, or other unrecognized reference-bearing representation makes this correction MANUAL_REVIEW with a limitation. Shared/array/dynamic formulas must either be parsed and proven safe as a group or make rename manual; Phase 4’s minimum safe profile refuses them. Do not guess whether an unsupported structure happens to mention the sheet. A HYPERLINK formula that can encode an internal destination in a string also makes rename manual; do not leave a stale sheet address inside a protected string literal. Other safe corrections may still proceed if those untouched parts remain byte-identical. The required supported rename fixtures include ordinary formulas, defined names, chart series, quoted sheet names, data validation, conditional-formatting references, and internal hyperlink locations. A name-only edit is never an acceptable substitute.

### 8.5 Hyperlink label correction

Support one simple worksheet hyperlink at a single cell whose value is a plain string, shared string, inline string, or empty text cell, with no formula, rich-text runs, merge, or competing hyperlink declaration. Replace the cell’s displayed text with the approved text as an inline string; preserve cell address, style, hyperlinks, relationship destination, location, tooltip, and all other cells. Set the hyperlink `display` attribute to the same label if present or needed for consistency. Never modify a shared-string entry used by other cells; leave the shared-string table untouched. Do not add a leading apostrophe to user text or interpret `=`, `+`, `-`, or `@` as a formula: an explicit inline-string cell is always text.

The supplied text must no longer match the ambiguous-label predicate. Destination semantics stay unchanged and destinations are never visited. Formula-based, rich-text, multi-cell, drawing, ambiguous, and merged hyperlink representations are MANUAL_REVIEW. A semantic change to one approved label is allowed; no other cell value may change.

### 8.6 Confirming a header row

The author may confirm only the first row of the exact reported rectangle. Do not accept a different row or replacement labels. A safe candidate has at least two rows and two columns; distinct nonblank plain-string first-row labels of at most 255 code points; no formulas or rich text in the proposed header cells, and no merged cells, table overlap, existing auto-filter overlap, or unsupported feature attached to the rectangle. A candidate may contain ordinary formulas in body cells if the header operation leaves their text, caches, and styles unchanged.

For an unstructured dense candidate, create a minimal valid SpreadsheetML table over that exact rectangle with `headerRowCount=1`, matching table-column names, no totals row, no filter, and no style overlay. Assign the lowest unused positive numeric table ID across the workbook and the first available `A11yTable<ID>` name, adding `_1`, `_2`, and so on if it collides case-insensitively with a table or defined name. Add the required worksheet relationship, tablePart reference, content-type declaration, and table XML in schema-valid order. Select the lowest unused positive worksheet relationship number with prefix `rId`; select the lowest unused `xl/tables/tableN.xml` path. Do not change cell values, formulas, formatting, table selection, or insert rows.

For an existing formal table with disabled header, support enabling it only when its existing first-row plain-string cells already match its existing distinct table-column names, there is at least one body row after that row, and no merge, totals row, query binding, extension, or header-dependent structured reference makes the change ambiguous. Change only headerRowCount to 1 and associated header metadata if necessary; do not consume a data row by inventing header labels. Other table defects are manual review. This deliberately implements technically safe header marking while preserving the Phase 2 requirement that logical table organization needs human judgment.

### 8.7 No selected edits

A valid batch containing only DECLINE and/or MANUAL_REVIEW is accepted and recorded. It creates a separate version and downloadable output with byte-identical workbook bytes, plus a fresh output audit and report. Do not require the output hash to differ; distinct version identity and preservation are the requirements. Report zero approved changes and retain unresolved findings. An APPLY that causes no effective correction is rejected before acceptance. Repeating a completed accepted request with its idempotency key returns its original acceptance response and does not create another output.

## 9 State transitions and remediation lifecycle

Audit status is derived once at successful completion: no findings gives AUDIT_COMPLETE; any REQUIRES_USER_INPUT or future AUTOMATICALLY_REMEDIABLE finding gives REMEDIATION_AVAILABLE; findings that are all MANUAL_REVIEW give REVIEW_REQUIRED. Limitations alone do not force REVIEW_REQUIRED. A finding with an unavailable safe editor must be manual, not advertised as remediable. Any audit execution failure gives PROCESSING_FAILED with no partial results. Historical Phase 3 statuses remain as recorded under ruleset 1.0.0.

For uploaded versions, PROCESSING reflects the latest active audit; on completion use that audit’s status. For generated versions, the successfully committed output starts as REMEDIATED, even when its output audit still has unresolved findings; REMEDIATED means an approved output was generated, not clean or compliant. A later re-audit temporarily makes its version PROCESSING, then returns it to REMEDIATED on successful audit, or PROCESSING_FAILED if that latest audit fails. Its audit body still reports actual remaining issues. Never use REMEDIATING as the persistent source-version status; that state belongs to the separate remediation record. The enum can remain in the shared model for compatibility.

Operation 7 reserves a durable remediation and job, canonicalizes and stores the decision set, and returns exactly `remediationId`, `sourceVersionId`, and `status: "REMEDIATING"`. The source’s bytes and existing audit records stay unchanged. Its stable version status is not rewritten by a branching remediation. Do not invent output IDs in the acceptance response.

Operation 8 returns exactly `remediationId`, `status`, `sourceVersionId`, `outputVersionId`, `outputId`, `auditId`, and `error`. REMEDIATING has all three output/audit fields null and null error. REMEDIATED requires all three nonnull and null error. PROCESSING_FAILED requires null output fields and a ProjectError. A polling GET returns 200 for all three states; the body carries processing success or failure. A project lookup/authentication error still uses an HTTP error status.

The worker reads and verifies the immutable source, resolves all approved changes, edits a private candidate, validates package structure and limits, runs preservation checks, runs all six rules against candidate bytes, confirms approved corrections took effect, and publishes only after every stage succeeds. The output audit can legitimately report unrelated or declined findings. Success atomically commits the output version, output record, successful output audit, report snapshot, and terminal remediation state. Its new audit uses the output hash and ruleset 2.0.0; finding IDs normally change when bytes change. Do not compare source and output finding IDs to determine resolution. Resolve approved effects using stable source targets plus the known rename mapping.

On any failure, retain the source, source audits, and failed remediation/error. Remove or quarantine unpublished candidate blobs and publish no successful version/output. An output-audit failure fails the remediation even if the ZIP was generated successfully. Subsequent correction attempts require a fresh key and new remediation. One active operation per source version prevents a re-audit racing an accepted decision batch. Different versions may proceed independently; serialize durable worker execution as needed for the host budget.

### 9.1 Idempotency and recovery

Operation 7 requires `Idempotency-Key`, a valid Identifier scoped to authenticated owner, namespace, HTTP method, and full path. Missing or malformed key returns 400 IDEMPOTENCY_KEY_REQUIRED. Before reserving a job, store the key, a canonical request digest, and the exact 202 response in the same transaction. Canonicalize by sorting decisions by findingId, sorting JSON object keys, using compact UTF-8 JSON, and preserving accepted string content. Same scope/key/body returns the original 202 acceptance response even after completion or failure. Same scope/key with a different body returns 409 IDEMPOTENCY_CONFLICT. Persist keys for the life of the workbook/session; do not expire them during Phase 4.

Validate and reject invalid requests without consuming the key. For valid requests, an existing key lookup precedes the active-operation check, so a legitimate replay is not rejected as a competitor. The tester does not automatically retry mutations; it exercises this replay behavior in a dedicated case. Other POSTs remain non-idempotent: do not automatically retry an uncertain upload, re-audit, system setup, tester setup, or run. Recover a lost remediation acceptance by explicitly replaying its identical keyed request; recover a lost tester run response by GET report.

Jobs use transactional claims, durable leases, and terminal transitions that reject stale workers. Never replay a partially applied candidate against source storage. A worker crash or lease expiry fails the accepted operation with a controlled error and no published partial output; it does not leave REMEDIATING forever. A late worker cannot overwrite a failure or a newer result. Session cleanup refuses active jobs and then removes all session-owned source/output blobs, reports, histories, measurements, keys, and decision records without touching ordinary data.

## 10 Errors limits and timing

| Condition | HTTP | New Phase 4 code |
|---|---|---|
| Missing or invalid bearer | 401 | UNAUTHORIZED |
| Wrong role or foreign owner | 403 | FORBIDDEN |
| Unknown/mismatched resource | 404 | NOT_FOUND, or FINDING_NOT_FOUND for a decision finding |
| Malformed request | 400 | INVALID_REQUEST |
| Required APPLY value missing | 400 | MISSING_VALUE |
| Duplicate decision | 400 | DUPLICATE_DECISION |
| Missing/invalid remediation key | 400 | IDEMPOTENCY_KEY_REQUIRED |
| JSON request body over 1 MiB | 413 | REQUEST_TOO_LARGE |
| Synchronous response exceeds JSON bound | 500 | RESULT_TOO_LARGE |
| Latest audit not completed successfully or stale audit | 409 | AUDIT_NOT_READY or STALE_AUDIT |
| Active competing audit/remediation | 409 | ACTIVE_OPERATION |
| Different body reuses key | 409 | IDEMPOTENCY_CONFLICT |
| Known output not published | 409 | OUTPUT_NOT_READY |
| Report audit still processing | 409 | REPORT_NOT_READY |
| Legacy audit has no report snapshot | 409 | LEGACY_REPORT_UNAVAILABLE |
| Legacy audit has no correction context | 409 | LEGACY_REVIEW_UNAVAILABLE |
| Invalid correction content/shape | 422 | INVALID_CORRECTION |
| Incompatible choices in one batch | 422 | CONFLICTING_CORRECTIONS |
| APPLY outside safe profile | 422 | UNSUPPORTED_CORRECTION |
| Persistence/init failure before acceptance | 500 | STORAGE_FAILED or INTERNAL_ERROR |
| Stored source/output checksum mismatch | 500 | SOURCE_INTEGRITY_FAILED |
| Dependencies unavailable | 503 | SERVICE_UNAVAILABLE |

For active-operation and stale-audit conflicts, check active operation first, then latest-audit identity, then successful-terminal status. Apply these checks after resource authorization. If a body supplies several invalid decisions, examine them in input order and report the first error; do not perform any edits. Do not use HTTP 202 for a rejected request.

Asynchronous ProjectError codes include PROCESSING_TIMEOUT, WORKER_LOST, PRESERVATION_FAILED, OUTPUT_VALIDATION_FAILED, OUTPUT_AUDIT_FAILED, ENGINE_RESULT_INVALID, and STORAGE_FAILED. Include a useful safe explanation; do not fabricate successful empty findings after a crash. A failed report identifies the failed stage. These asynchronous errors appear in a 200 status response body, not as a polling HTTP 500.

| Bound | Phase 4 value |
|---|---|
| File upload bytes and intake ZIP limits | Section 6, unchanged |
| Accepted decision count / JSON request body | 1,000 / 1,048,576 bytes |
| Validation / audit execution | 30 s / 60 s |
| Remediation total worker execution including validation, comparison, and output audit | 180 s |
| Queue wait before claiming any operation | 15 s |
| Tester upload / ordinary business GET | 35 s / 10 s |
| Tester binary or report download | 30 s |
| Tester polling deadline for one audit / remediation | 80 s / 210 s from acceptance |
| System setup / tester setup | 30 s / 45 s |
| Whole Phase 4 suite / caller and proxy run read timeout | 1,800 s / at least 1,830 s |
| Tester JSON response / binary workbook response | 67,108,864 bytes / 33,554,432 bytes |
| System serialized audit/report result | 67,108,864 bytes |

The tester and system must both support these limits. They are shared external timing and transport bounds; internal process isolation, memory budgets, storage adapters, and IPC formats are implementation choices. A generated XLSX remains no larger than 25,000,000 bytes and obeys all source package/content limits. The larger binary transport cap permits error detection without arbitrary failure at 2 MiB. Do not expand an output beyond intake limits and then advertise a successful downloadable result.

Use monotonic deadlines inside each request and the overall suite. Poll at approximately one second; never extend a deadline on every poll. Retry only safe GET transport failures, at most three attempts total within the same deadline. Do not follow redirects or disable TLS verification. New JSON bodies above the 1 MiB request bound return 413 REQUEST_TOO_LARGE. Oversized synchronous JSON responses return 500 RESULT_TOO_LARGE instead of a truncated successful response. An audit/report result exceeding its bound causes a controlled failure; never truncate findings into a successful partial audit. These resource limits do not excuse swallowing findings or skipping rules. Record limit-triggered failures and revisit limits with evidence if normal supported fixtures cannot complete.

## 11 Preservation and editing architecture

Use targeted OOXML edits for output generation. A general workbook-library load/save cycle does not establish preservation. The editor must preserve every unselected package entry and verify the permitted differences independently, including reference rewrites required by a worksheet rename.

Before editing, build a source inventory and an explicit allowlist of semantic changes for the approved decisions. Every package entry not required by an approved correction must have byte-identical uncompressed contents in the output. Compare entry path sets and the SHA256 of each decompressed entry, not only workbook-level values. Preserve compression type and relevant ZIP metadata where possible. Changed XML entries are compared semantically against the explicit approved-change allowlist; a required sheet-reference rewrite counts as an approved dependency of the rename, not an unexplained change. ZIP central-directory offsets, CRC values for changed entries, compression bytes, and timestamps need not make the entire archive byte-identical after an approved edit. Do not require cross-team output ZIP hashes to match; require each output’s declared hash to match its own downloaded bytes and require semantic correctness.

Preserve the source package’s Strict or Transitional namespace/relationship family, and create any new table structures in that same family. For changed XML parts, retain all unknown elements, attributes, namespaces, extension lists, markup-compatibility declarations, order constraints, relationships, and comments unless a selected correction explicitly requires a change. A serializer that drops unknown markup or produces undeclared namespace prefixes is not safe. If preservation cannot be established, refuse the affected correction before acceptance when detectable, or fail the accepted job at the preservation stage. Never silently remove unsupported parts to make an output open.

The independent semantic comparison covers: cell values/types/formulas and cached values; style definitions and assignments; row/column dimensions and visibility; merges; sheet order/IDs/names/visibility; defined names and scopes; chart XML, series references, drawings, media and anchors; hyperlinks and destinations; comments/VML; data validation; conditional formatting; tables/filters and ranges; relationships, content types, and opaque package parts. Do not compare only style indices or object counts. Unknown untouched parts must be byte-identical. Any altered reference must point to the intended same logical object after a rename. Preserve formula caches unless an approved edit necessarily changes a formula token; even then preserve the cached value because the cell’s meaning did not change. Do not mark the workbook for a full recalculation as a substitute for reference correctness.

Allowed differences are narrow: selected title/description attributes; selected sheet name plus required reference-token rewrites; selected hyperlink label cell/display attribute; or selected header/table metadata and required relationship/content-type additions. Structural additions must match the declared header operation. Everything else must compare equal. Verify selected corrections by re-reading with an independent safe parser and by the output audit, not by trusting the editor’s return value.

Signed packages cannot be altered without invalidating signatures; classify their corrections manual and disclose this. Preserve unsupported embedded objects byte-for-byte during unrelated safe corrections when the changed part can be serialized without affecting them. If a correction would touch an opaque structure, refuse that correction rather than claiming broad preservation.

Human Excel checks remain a release gate: open each supported correction fixture’s output in Microsoft Excel and record no corruption or repair warning. Automated validation is necessary but cannot stand in for that evidence. Retain the source/output hashes, approved change list, zero-unexplained-difference comparison, Excel version/platform, date, reviewer, and observed result. This specification does not claim those checks have occurred.

## 12 Downloadable audit reports

The report path in Section 5 returns a UTF-8 JSON attachment named `audit-<auditId>.json`, `application/json`, and `Cache-Control: no-store`. JSON is the required common report format; a UI may offer an additional human-readable view, but a server PDF is not required. Finalize a report snapshot for every terminal audit and return the same bytes on repeated download. PROCESSING returns 409 REPORT_NOT_READY. A failed terminal audit still has a report with its error and limitations; do not call it a successful audit.

AuditReport contains exactly the fields below. The schema in the companion pack defines types and required keys; this prose supplies the semantics.

| Field | Meaning |
|---|---|
| reportVersion, specificationVersion | reportVersion is `2.0.0`; specificationVersion is `2.1.0` |
| workbookId, versionId, auditId | Exact evaluated workbook/version/audit |
| fileName, sha256, sourceVersionId | Evaluated version’s filename, bytes hash, nullable direct parent |
| auditDate, rulesetVersion, status | Audit completion UTC, actual ruleset, terminal audit status |
| checksPerformed | Six rule IDs in the fixed order from Section 7, or the completed subset on failure |
| findings | Exact finding array from this audit |
| decisions | Canonical source decision records, empty when no originating remediation |
| approvedChanges | APPLY decisions actually committed, in source finding order |
| unresolvedFindings | Exact copy of all findings in this evaluated output/audit; no unsupported claim that consent resolved a finding |
| manualReviewItems | Subset of those findings classified MANUAL_REVIEW |
| limitations | Exact audit limitations, including applicable scope/display assumptions |
| remediationId, outputVersionId, outputId | Nullable provenance for generated output |
| error | Same nullable ProjectError as the audit |

A report decision has `sourceAuditId`, `findingId`, `ruleId`, `worksheet`, `target`, `action`, and nullable `value`. Report decisions are sorted by source finding order, not incoming request order. Report decisions use `value: null` for DECLINE/MANUAL_REVIEW; this differs intentionally from omission in input. ApprovedChanges uses the same record shape and contains only committed APPLY records. SourceAuditId and findingId refer to the source audit; do not replace them with the output audit’s new hashes. For an ordinary upload/manual re-audit with no originating remediation, provenance fields are null and decision/change arrays empty. A later re-audit of a generated version retains the version’s originating remediation and committed change provenance.

All remaining findings are unresolved, including declines and manual review. A confirmed decorative object remains a manual finding unless the rule’s required workbook metadata changes; recording review is not suppressing the rule. Findings and limitations are version-specific and are not retroactively changed on the source report. OutputReport may have different worksheet names/targets after a rename. Never state that all six rules ran if execution failed before completing all six.

## 13 User interface behavior

Keep upload/results functionality and add workbook history, new-version upload, manual re-audit, decision entry, remediation polling, and both downloads. A finding shows location, explanation, recommendation, classification, and permitted actions. For alt text show separate title and description fields. A header action shows the exact table rectangle and row being confirmed. A rename preview warns that references are updated. Manual-only findings do not offer APPLY. Default every choice to unselected; do not preapprove all edits. The user explicitly submits selected decisions before any candidate is generated.

Show the exact source version and audit selected, pending job state, server validation errors near the affected field, and post-remediation unresolved items. A reviewer sees read-only controls. Handle 409 conflicts by refreshing the latest version/audit and preserving unsent user text locally in memory. Do not silently move decisions to a newer audit. A timed-out request is not proof that processing failed. Store the operation 7 idempotency key and exact submitted body until its acceptance is recovered; do not generate a new key on each network retry.

Do not put tokens in URLs or retained reports. Keep tokens in memory rather than browser persistent storage by default. Render filenames, messages, alt text, and labels as text, not HTML. Use programmatic labels, visible focus, keyboard navigation, text errors, and announced status updates while building these features. Phase 5’s full accessibility assessment remains separate; Phase 4 work must not introduce inaccessible review controls.

## 14 System and tester interface

Keep the instructor wire format unchanged. The registered system exposes POST `/test/setup` and GET `/test/report`. The separately addressable tester exposes POST `/test/setup`, POST `/test/run`, and GET `/test/report`. Do not create a system `/test/run` or add Phase 4 options/tokens to the grader setup body.

| Caller and operation | Body or query | Success |
|---|---|---|
| Operator to tester setup | `{target_url}` | 201 `{session_id}` |
| Tester to system setup | `{session_id,setup:{}}` | 201 echo of the same session ID |
| Operator to tester run | `{session_id}` | 200 SuiteReport after completion |
| Operator to tester report | `?session_id=<id>` | 200 stored SuiteReport; 409 until ended |
| Tester to system report | `?session_id=<id>` | 200 `{session_id,coverage:{code:{covered,total,basis}}}` |

Tester setup snapshots the manifest, fixture bytes/hashes, expectations, and ruleset before preparing an empty isolated namespace on the target. The tester always sends setup explicitly as `{}`; the system preserves the existing default that omitted setup is an empty object. System setup supports `cleanup_session_ids`, an array of earlier namespace IDs owned by the caller, and the optional `faults` object defined in Section 14.1. Other setup keys return 400. Unknown earlier IDs are idempotent no-ops; an existing foreign or active namespace is refused without touching its records. Duplicate system setup IDs return 409, and unknown report sessions return 404. No suite cases execute during setup. Only one run is allowed per prepared session. Concurrent, repeated, and post-abort runs return 409. Failed setup creates no runnable session. A valid target setup error preserves its status/message/source through the tester. Invalid request shapes do not consume sessions. GET requests have no body.

Testing endpoint errors remain exactly `{message,source}` with source `tester`, `system`, `communication`, or `unknown`. Use 400 invalid request, 401 invalid credentials, 403 forbidden, 404 unknown session, 409 conflict/not ready, and 500 service failure/abort. Do not substitute the four-field ProjectError here. A tester 200 means it completed reporting; it may contain failed/error cases. A tester abort returns 500 while retaining an available final report with completed results, an interrupted tester error, and skips for unexecuted cases.

SuiteReport remains exactly `session_id`, `target_url`, `results`, and `coverage`. Each result is `{test_id,outcome,message}`; include `source` exactly when outcome is `error`, never for pass/fail/skip. Every planned ID appears exactly once in manifest order. `pass` includes an expected rejection. `fail` means an evaluable assertion disagreed. `error` means evaluation could not proceed because of an unexpected system, tester, communication, or unknown problem. `skip` means the case did not execute, with an explicit prerequisite or observation reason. Never turn a failed prerequisite into a dependent pass. Continue independent evaluable cases after an earlier failure.

Store the final serialized report before returning run. Subsequent report GETs return the stored report unchanged. Tester reports persist in the separate tester database across an application restart/deployment. Retain a run record outside the instructor response for manifest/spec versions, fixture hashes, target/tester revision, wall-clock start/end, retrieval timestamp, counts, operation conformance, acceptance evidence, and unresolved discrepancies. Do not add these fields to SuiteReport.

### 14.1 Controlled audit failure

The designated shared test deployment must support a test-only audit failure switch enabled by trusted server configuration. It is disabled by default outside that designated deployment. Only a valid test-admin credential may configure it through system POST `/test/setup`, and only while creating a fresh isolated session. It never applies to ordinary user data, another session, or an existing session. No public fault endpoint, file-name trigger, special workbook bytes, client-selected exception, or arbitrary code execution is allowed.

The exact request is:

```json
{
  "session_id": "failure-session-001",
  "setup": {
    "faults": {
      "audit_failures": 1
    }
  }
}
```

The response remains 201 with only the echoed session_id. `faults` permits exactly the required integer field `audit_failures` with value 1. Strings, booleans, zero, other counts, empty faults, and unknown fault fields return testing error 400 with source system. If the trusted switch is disabled, a well-formed fault request returns testing error 403 with source system and creates no runnable session; the tester records this as a failed required capability check rather than an observation skip. A caller lacking the test-admin role receives the normal testing 403; missing/invalid authentication receives 401. Validate fault authorization and shape before performing cleanup or creating session state.

Atomically assign and consume the fault when the first valid upload or manual re-audit in that session is accepted. Rejected input does not consume it. The job must still be durably queued, and the upload/re-audit returns the normal 202 identifiers. After claiming the job and verifying source integrity, the worker deliberately completes that audit as PROCESSING_FAILED before evaluating accessibility rules. It sets completedAt, exposes no partial findings, and returns ProjectError code TEST_AUDIT_FAILURE with message `Audit failure requested by the isolated test session.` and details `{"stage":"audit","testOnly":true}`. requestId is a valid server-generated request/job identifier. The corresponding version records the failed latest audit and its original source bytes remain retrievable and unchanged. The audit’s failed report has checksPerformed empty and the same error.

The fault is one-shot and durable across restart: a subsequent manual re-audit in the same session runs normally. A storage or source-integrity failure still reports its genuine cause; the injected error must not conceal it. Output-remediation audits cannot consume this control. Faults cannot be rearmed by repeating setup, because a reused session ID returns 409.

The main suite session is prepared with empty setup. During the failure test, the tester prepares a separate fresh fault session using the configured test-admin identity, uploads an independently reviewed valid clean fixture with that session header, and polls for the exact injected failure. It verifies the complete failed audit envelope, failed report, source preservation, and subsequent successful manual re-audit. Store the fault-session ID with the run evidence. Retain its evidence until the run is recorded, then use caller-owned inactive-session cleanup under the existing rules. The primary session’s code coverage excludes this separate session unless the system explicitly provides a verifiable combined scope; never add its line counts by assumption. The instructor SuiteReport schema stays unchanged.

## 15 Tester implementation and fixture contracts

The tester remains independent of production audit and correction implementations. It may share wire schema definitions, but it must not call production rule code, an editor, or the system’s preservation comparator to calculate expected results. Its own package comparator, independently checked finding-ID calculation, and reviewed fixtures supply the oracles. A simulated target may test the tester itself; only real system/worker runs establish integration behavior.

### 15.1 Test suite structure

A suite consists of a versioned case manifest, immutable fixture bytes, an independent expectations file, check handlers, an HTTP client, a dependency-aware runner, a downloaded-workbook comparator, and retained result storage. Each manifest entry declares a unique test ID, check handler, prerequisites, fixture parameters, expected behavior, qualifying assertions, and component/acceptance/operation mappings. The same suite must accept either registered implementation as its target without changing its oracles.

The operation catalog keeps business operations 1 through 14 and the additional S, R, and V paths. A report describes the features delivered in this phase rather than marking all additions as future work. Exact target URL allowlists, private access provisioning, TLS verification, base-path handling, response bounds, no redirects, and bounded safe GET retries apply to every handler. A target revision is deployment evidence supplied by the operator, never inferred from the tester’s own build.

The companion `phase4-test-plan.json` is a normative assertion inventory, not a runnable suite implementation. A runnable manifest connects its assertions to implemented handlers, concrete reviewed fixtures, real resource prerequisites, and a frozen execution order. Historical case IDs may be retained when their assertion remains the same; fixture expectations must match the declared ruleset. A required failure-envelope case uses the controlled mechanism in Section 14.1 instead of waiting for an accidental crash.

### 15.2 Fixtures and independent expectations

Every committed fixture records: key, relative path, origin team/synthetic, raw byte SHA256, upload name, ruleset version, expected intake status/error, exact semantic findings, exact canonical targets, expected completion status, expected limitations or required limitation facts, review names/date, and Excel-open result where applicable. Reject a changed byte hash at setup before creating a runnable session. An expected finding’s ID is independently computed from those fields. Do not store dynamic resource IDs or server prose as a cross-team oracle.

At minimum include `clean_all_six`, `seeded_all_six`, `review_only`, `alt_supported`, `rename_references`, `link_supported`, `header_range`, `header_disabled_table`, `preservation_rich`, `unsupported_edits`, `mixed_decisions`, and all retained intake/duplicate-ID/Unicode/unsupported-object edge fixtures. Clean means no triggered findings under all six rules, not simply the old two. Seeded includes each category, both user-input and manual items, a decline, a limitation, and supported edit targets. The all-six seeded expected array is written from the fixture’s actual XML, not by running an engine.

Rename fixtures cover quoted and unquoted references, apostrophes, named ranges, chart series, validation, conditional-formatting formulas, internal links, and a same-name string literal that must not change. Separate unsupported fixtures cover 3D/dynamic/external references. Header fixtures prove table relationships/content types and unchanged body cells. Link fixtures include shared-string reuse and a formula-prefixed approved label stored as text. Preservation fixture includes formulas/caches, styles, names, drawings/media, comments, relationships, validation, conditional formatting, hidden sheets, and at least one untouched opaque feature.

Each implementation’s answer key must be reviewed by two members of the other implementation team. Review includes fixture byte hashes, all six rules, target paths, classifications, expected statuses, correction effects, and allowed preservation differences. Record reviewer names, date, and decisions for each fixture pack; approval is not implied by a successful self-test. Use the same adopted pack for reciprocal testing and keep alternative/synthetic packs separately instead of overwriting their bytes.

### 15.3 Comparisons and execution

Validate every response schema, namespace/ownership, IDs, states, and relevant byte hash before asserting findings. Compare exact semantic tuples `(findingId,ruleId,category,worksheet,target,classification)` in canonical order. Check messages/recommendations are present, bounded, accurate by human review, and stable within one target. Limitations must be deterministic, sorted, nonduplicated, and disclose oracle-required facts; cross-team wording need not be identical. A failure of detection accuracy must not prevent determinism comparisons if the target supplied a valid completed audit.

Repeat identical bytes five times and compare findings, limitations, statuses, and within-target prose. Separately test five manual re-audits of one version. Do not compare audit IDs/timestamps or require different uploads to reuse IDs. Test post-remediation output bytes independently: verify approved changes, absence of declined/unselected edits, raw source hash preservation, output metadata hash, and zero unexplained semantic changes. Output ZIP serialization may differ across teams.

Parameterize each negative/auth case into an individual stable test ID. A manifest case must not hide several unreported assertions with different outcomes. Every test declares exact expected status/shape or independent artifact comparison, real prerequisites, and a qualifying assertion for coverage. Dependencies reflect required resources, not whether an unrelated accuracy check passed. Cases after a failure still run when their own inputs exist.

## 16 Phase 4 acceptance and coverage

| Criterion | Phase 4 evidence |
|---|---|
| AC 01 and 02 | Retained intake and boundary regressions, including chartsheet counting |
| AC 03 | Source download bytes/hash unchanged after audit and remediation; separate output version |
| AC 04 | Exact all-six seeded findings and zero clean-fixture findings |
| AC 05 | Schema, location, classification, explanation and recommendation review |
| AC 06 | No invented meaning, explicit input/manual treatment, no edits before consent |
| AC 07 | Each supported correction; approve one and decline another; only selected changes |
| AC 08 | Authorized separate XLSX and JSON-report downloads linked to correct versions |
| AC 09 | Independent semantic preservation plus real Microsoft Excel no-repair inspection |
| AC 10 | Complete version-specific report with decisions, committed changes, unresolved/manual items and limitations |
| AC 11 | Multiple uploads, generated version, re-audits, legal transitions and unchanged old audits/reports |

The shared suite catalog is C1 authentication/access; C2 intake; C3 immutable sources/version metadata; C4 six-rule audit engine; C5 durable jobs/audit API; C6 isolated sessions/measurement; C7 decisions/remediation; C8 downloads/reports/history; and C9 review UI. Component coverage counts an executed qualifying assertion even if it fails; skipped paths earn no coverage. C9 requires retained browser/manual review evidence and is not counted by HTTP-only runs. HTTP reports therefore have component total 9 and covered at most 8 unless the runner actually performed a qualified UI check. State this in basis. Browser/manual release evidence remains separate from an HTTP-only SuiteReport and must not be injected into that run’s numerator after it is finalized.

System coverage catalog contains AC 01 through AC 11 inclusive, denominator 11. Count an AC when at least one mapped qualifying assertion executed, even if it failed, and state in basis that this is exercised-criterion coverage, not full acceptance success. AC 09 HTTP checks are only the automated preservation portion; Excel inspection remains an explicit acceptance gate. Do not inflate the numerator using manifest membership or a skipped test. Maintain a separate per-AC completion/evidence record that identifies outstanding human work.

Keep code, component, and system measures as `{covered,total,basis}`. Counts are nonnegative integers with covered <= total, or both null with a nonblank explanation. Zero is measured zero, not unavailable. Code coverage comes only from the system’s actual session measurement report, never from target source inspection or a fake constant. Extend measurement to remediation children if implemented and state the exact included paths; otherwise disclose that audit/validation-only measurement does not cover the new editor/API/storage/UI. Reading coverage must not execute work or reset counts. Do not combine measurements from different deployment revisions in one numerator.

Local integration checks must exercise invalid values, corruption, timeout, worker loss, preservation failure, and output-audit/storage failure. The registered Phase 4 test target must enable the isolated audit-failure control in Section 14.1. The failure-envelope case is required and must not skip simply because ordinary audits are healthy. Supplemental AC 14/15/18 failure, authorization, and determinism checks remain outside the eleven-AC Phase 4 denominator; they do not establish full Phase 5 completion.
