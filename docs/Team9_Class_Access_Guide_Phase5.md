# Team 9 Class Access Guide — Phase 5

Excel Accessibility Hub · November 16, 2026  
Specification / suite 3.0.0 · Audit ruleset 2.0.0

## Access and release status

This guide describes the combined Phase 5 source from PRs #16–#19. **Deploy the new
system/website and independent tester, with the matching worker, before hosted testing.**
Hosted Phase 5 validation remains pending until both releases and their private configuration
are confirmed. The addresses below are existing registered endpoints; a main merge or an
available health response alone does not establish that Phase 5 is active. Confirm the deployed
release SHA and tester suite 3.0.0 before a graded run. Local verification and the deployment
sequence are recorded in [the integration record](evidence/phase5_integration.md).

| Service | Address |
|---|---|
| Website | https://3.139.178.70.sslip.io/system/ |
| System base | https://3.139.178.70.sslip.io/system |
| System API documentation | https://3.139.178.70.sslip.io/system/docs |
| Tester base | https://3.139.178.70.sslip.io/tester |
| Tester API documentation | https://3.139.178.70.sslip.io/tester/docs |

The following application credentials are intentionally shared for class demonstrations and
testing. Use the tokens exactly as shown, without a `Bearer` prefix in the website or API
documentation's **Authorize** dialog. Other HTTP clients use `Authorization: Bearer TOKEN`.
Local installations use their own credentials.

| Identity | Token | Use |
|---|---|---|
| userA | `QMxgTEcsbuHJaVRV7M1h4NwEaPqeYMEEJHtV9o6OMPA` | First owner; upload, review, correct, schedule and delete owned workbooks |
| userB | `hRawDUIYyjy_glcn_NLxc6-QZPeWIdpy_-lLW6lSwIg` | Second owner; independent workbooks and ownership-isolation testing |
| reviewer | `P7jbZevd0ZZ7KzAoPICUG9E-DHz9MoATPgw4H0EtC_M` | Read-only access to userA's granted workbooks, schedules and notifications |
| test-admin | `7f5pJMFZF1vy5x_UU_SeXmG6UhdAFM3J1uZKw90PCb8` | Prepare/read owned isolated system sessions and advance their test clocks; no ordinary workbook access |
| operator | `nrTr9hMvQ472w9OfyfLLYxF7sjaN5_-WDfKXmjtVsBk` | Set up, run and read the independent tester; not a website identity |

The shared credential set has no second independent `test-adminB` token. The suite can run
without it; the two foreign-admin clock cases are skipped. Provision that additional identity
privately to exercise those cases. Do not substitute the existing test-admin token, because
the check requires a different administrator.

A test-admin gains no ordinary workbook access. An operator token is for the tester,
not the website. People using the same token share the same application identity.

Check system `GET /api/v1/health` relative to the base address. Available health is exactly:

```json
{"status":"AVAILABLE","apiVersion":"1.0","rulesetVersion":"2.0.0"}
```

Health has the same shape in Phase 4 and Phase 5. Confirm schedule/notification/delete/clock
operations in the deployed API catalog and confirm that the tester operator selected suite
3.0.0. Health alone does not verify deployment, the tester manifest or model coverage.

## Upload and review a workbook

1. Open the website. Enter **userA's private token** in **User access token**,
   without a `Bearer` prefix, then choose **Connect**.
2. Select an Excel `.xlsx` file. Keep **Upload destination** as **New workbook**, then choose
   **Upload and check**. Supported bounds are 25,000,000 file bytes, 50 worksheets and
   250,000 nonempty cells. Unsupported/protected/malformed files receive a text error.
3. Wait for processing. Save the displayed **workbook ID** and **version ID**. Read findings
   and the full limitations list.
4. To return later, enter the workbook ID in **Existing workbook ID** and choose **Open history**.
   Select the desired version/audit. **Refresh history** retrieves newly completed work.

The six checks cover alternative text, worksheet names, table headers, merged cells,
hyperlink text and color/contrast. Results support human review and are not a compliance
determination. An empty finding list is not a guarantee of accessibility.

`REMEDIATION_AVAILABLE` means supported changes await decisions. `REVIEW_REQUIRED` means
manual review is needed. `PROCESSING_FAILED` is a controlled failure; inspect its safe error.
A source and its earlier audit/report remain available after a failed attempt.

## Approve supported corrections and download

1. For each finding you want to address, select an action. **APPLY** requires your explicit
   text or confirmation: alternative-text title/description, a supported worksheet name,
   hyperlink label or safe table-header choice.
2. Use **DECLINE** to decline an edit or **MANUAL REVIEW** to record manual review.
   These actions do not change workbook content. Unselected findings remain unresolved.
3. Choose **Submit selected decisions** once and wait for verification. The system builds
   and checks a separate candidate before publishing it.
4. Use **Download remediated workbook** for the generated output and **Download audit report
   (JSON)** for the selected audit. **Review output findings** opens the output's own audit.
   **Download selected workbook** retrieves the version currently selected in history.

Supported edits are bounded; unsafe/unsupported cases remain manual findings. The system
never invents alternative text, runs macros or follows link destinations. `REMEDIATED`
means an output was committed and may still contain unresolved findings. Original bytes
and historical report snapshots are preserved.

For external edits, select **New version of workbook in history** before uploading the edited
copy. **Re-audit selected version** adds an audit to that version; it does not make an older
version the newest stored version.

## Configure periodic audits

Open a workbook's history and find **Schedule and notifications**.

1. Set **Periodic audits** to **Enabled**.
2. Enter a whole-number **Interval in days (1–90)**.
3. Optionally enter the **Institution-supplied deadline** provided by the responsible
   institutional administrator. The application does not determine that deadline.
4. Choose **Save schedule** and check the summary's enabled state, selected stored version
   and next audit time. Use **Refresh schedule and notifications** to retrieve current values.

Saving replaces the whole configuration. Disabling periodic audits keeps deadline reminders;
use **Clear deadline** and save to stop reminders too. A deadline-only change or identical save
preserves the next audit time. Changing the interval or enabling again starts a new cadence.

**Scheduled audits check the stored version only. Changes to an external workbook cannot be
detected until a new version is uploaded.** The newest committed version includes a generated
output. Uploading a newer version does not postpone the schedule. Several missed intervals
produce one audit on the original cadence. A failed occurrence does not disable future audits.

Reviewers see schedule/notification summaries but cannot save settings or delete a workbook.
The simulated test clock is an administrator testing control and is absent from this website.
Ordinary workbook scheduling uses real UTC time.

## Read deadline reminders

**Notification history** lists durable in-application records, oldest first. Reminder thresholds
are UTC midnight 90, 30 and 7 calendar days before the supplied deadline. A successful clean
audit of the newest version suppresses a reminder; unresolved findings, failed/processing or
absent audits remain eligible.

**DELIVERED** means recorded in this application, not email delivery. **FAILED** records a
notification failure without changing the audit. Each deadline/offset occurrence is considered
at most once. Setting a deadline after an earlier threshold does not backfill it. Disabling
periodic auditing alone does not disable these reminders.

## Delete a workbook

1. Open the correct workbook's history and check its ID.
2. Choose **Delete workbook…**. Read the confirmation identifying that workbook.
3. Choose **Cancel** to keep it, or **Confirm deletion** to remove it.

A completed deletion removes every version, generated workbook, report, finding, decision,
schedule and notification for that workbook. Old URLs become unavailable. The backend also
purges registered application-managed copies; operators must follow the backup/deletion
journal procedure before enabling this release. Independent tester reports remain retained.

An active operation prevents deletion. Let it finish, refresh, then explicitly try again.
A partial storage cleanup can make the workbook unavailable while recovery continues.
Keep the tab open and follow **Resume pending operation** when offered; only the original
owner can explicitly resume its cleanup. Do not treat an error or lost response as proof
that the workbook remains readable. Deletion has no application undo.

## Run the independent Phase 5 tester

Coordinate one full grading window on the shared host. The tester operator must first configure
suite 3.0.0, approved exact targets, private target credentials and the trusted target-team map.
Own-team runs also require Team 9's complete design map. These are server settings, not fields
in the instructor request.

1. Open tester `/docs`, choose **Authorize**, and enter the private **operator** token
   without `Bearer`.
2. Execute `POST /test/setup` once with exactly this body for Team 9 self-testing:

   ```json
   {"target_url":"https://3.139.178.70.sslip.io/system"}
   ```

3. Require status **201**, then copy the returned `session_id`.
4. Execute `POST /test/run` **once** with:

   ```json
   {"session_id":"PASTE_RETURNED_SESSION_ID"}
   ```

5. Allow up to **2,400 seconds** for the suite. A script/client needs a read timeout of at
   least **2,430 seconds**; the source proxy is configured for 2,700 seconds.
6. Retrieve `GET /test/report` using the same session ID. Save its complete unchanged body.
   Report retrieval never runs tests. A completed report may contain failures/errors.

If the run response is lost, retrieve the report instead of repeating the run POST. Poll a
409 report response within the original deadline. Do not automatically repeat an uncertain
setup POST; a prepared session permits one run only. Setup takes up to 45 seconds in the tester
and 30 seconds in the target system.

| Report field | Meaning |
|---|---|
| `pass` | Executed assertions matched their oracle |
| `fail` | An evaluable mismatch |
| `error` | Evaluation was prevented; `source` identifies tester/system/communication/unknown |
| `skip` | The check did not execute; read the reason |
| `coverage.code` | Actual target instrumentation in its stated scope |
| `coverage.component` / `system` | Evaluated shared components / AC-01 through AC-18 |
| `coverage.model` | Required for own-team runs; absent entirely for cross-team runs |

There are **395 planned hosted cases**. Read the actual counts and skips in the stored report.
Coverage counts evaluated scope, including failed assertions; it does not mean acceptance.
An HTTP-only run cannot earn UI component C9 or AC-17. Team 9's measured code scope is
`app/workbooks` and `app/audit`, excluding API, storage and UI.

The existing Team 12 target is `https://13.184.14.226.sslip.io`. Use it only after confirming
its Phase 5 release/capabilities and the private server configuration. Keep using Team 9's
operator token for Team 9's tester. Target credentials never go in the setup body.
Reciprocal testing remains separate from local integration evidence.

## Recovery and remaining acceptance

Credentials, drafts and pending recovery are kept in the browser tab's memory. Keep the tab
open after uncertain remediation/schedule/deletion acceptance and follow the explicit recovery
control. After an uncertain upload/re-audit, inspect history before retrying. Changing credentials
clears the page's review state. Reconnect with a granted identity if access is denied.

This source integration does not establish human Excel no-repair inspection, other-team fixture
review, keyboard/assistive-technology acceptance, five-real-workbook precision, live backup/S3
restoration or pod adoption. See the integration record and
[manual UI checklist](evidence/phase5/ryan_manual_validation.md) for the separate evidence records.
