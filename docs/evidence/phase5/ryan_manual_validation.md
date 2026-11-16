# Ryan Phase 5 manual interface validation

Status: **PENDING human execution**. Automated Chromium keyboard assertions and axe
scans are separate evidence. They do not satisfy the actual manual inspection.
No human result is inferred from this checklist.

Record before starting:

- Reviewer and date: PENDING
- Tested revision/overlay hash and URL: PENDING
- Operating system, browser/version, zoom/display settings: PENDING
- Assistive technology/version, if actually used: PENDING

Use an owner token, a granted reviewer token and disposable workbooks in the
approved environment. Keep credentials out of the record. Use only the keyboard
throughout; record failures, unexpected focus moves and recovery steps.

| Flow | Human checks | Result/notes |
| --- | --- | --- |
| Connect | Reach labeled token input, submit; identity/status announced; changing identity clears prior workbook state | PENDING |
| Upload/history | Select an .xlsx, upload, follow processing status, open old and new versions/audits without losing keyboard position | PENDING |
| Findings/corrections | Reach each decision and value field; read current values/errors; explicit APPLY/DECLINE; manual-only rows never offer APPLY | PENDING |
| Output/download | Reach output findings, source and JSON report downloads; understand output is a separate version and scope is limited | PENDING |
| Schedule | Enable; save 1 and 90 days; invalid/empty interval gives understandable feedback; identify scheduled version and UTC next due time | PENDING |
| Deadline | Set institution-supplied date; disable periodic audits and observe retained deadline; explicitly clear deadline and save | PENDING |
| Notifications | Understand empty history, recorded version, threshold and DELIVERED/FAILED text; delivery does not imply email | PENDING |
| Disclosure | Read stored-version/external-change limitation on scheduling and results views | PENDING |
| Delete/cancel | Confirmation names the workbook; Cancel initially focused; Tab/Shift+Tab stay in dialog; Escape returns to trigger; cancel changes nothing | PENDING |
| Delete/recover | Confirm disposable workbook deletion; success focuses existing-workbook input; blocked/failure states are readable and explicit retry works without duplicate automatic DELETE | PENDING |
| Reviewer | Open granted workbook; read schedules/notifications, download; no owner schedule/deletion/edit controls | PENDING |
| Errors/navigation | Foreign ID, invalid fields and network interruption give visible text/live announcements; no keyboard trap; refresh recovers safely | PENDING |
| Focus/contrast | All interactive controls have visible focus; normal text >=4.5:1, large text >=3:1; inspect scanner incomplete findings, focus ring and actual display rendering | PENDING |
| Responsive layout | At narrow width and 200% zoom, all controls and long identifiers remain readable/reachable | PENDING |

Attach actual observations, screenshots where useful, and a defect/retest record.
Sign `P5-MANUAL-KEYBOARD`, `P5-MANUAL-A11Y-SCAN` (human review of retained scanner
and contrast results), and `P5-MANUAL-EXTERNAL-DISCLOSURE` only after completing
those checks. A browser test pressing Tab/Escape is not a person's inspection.

Other release owners still need pod adoption/design-map approval, independent
human fixture review, Excel no-repair checks, adjudicated real-workbook precision,
hosted reciprocal runs, and live backup/deletion/monitoring evidence. This file
neither changes the shared adoption record nor claims those tasks are complete.
