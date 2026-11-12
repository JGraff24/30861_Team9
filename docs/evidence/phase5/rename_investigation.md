# Worksheet-rename discrepancy: independent-test-side investigation

Dixon (Team 9 tester), November 12 2026. Specification 3.0.0 §2 keeps the worksheet-renaming
mismatch reported against Team 12 open as a joint investigation and forbids silently changing
rules, finding identities or expected results to erase it. Nothing below changes any rule,
fixture, expectation, finding target or suite case.

## What is and is not on record here

- The repository holds no copy of the failing report or its session ID; only the pack's
  statement that the mismatch exists. The original report bytes, the failing test ID(s) and the
  message are needed and must be preserved unchanged (ADOPTION.md: "Preserve prior reports and
  fixture identities").
- The normative case is `P4-APPLY-RENAME` (fixture `rename_references`, plan `rename`,
  expectations in `tests/fixtures/expectations-phase4.json`, retained unchanged in suite 3.0.0).

## What the tester asserts for a rename (and where a correct target could differ)

| Tester assertion | Source | Could a conforming target legitimately differ? |
|---|---|---|
| The audit offers WORKSHEET_NAME at `xl/worksheets/sheet1.xml#sheetId=1` as `REQUIRES_USER_INPUT` | §7.3, §8.4 | **Yes, possibly.** The fixture's cross-sheet conditional formula lives in the x14 extension (`xm:f`), as Excel 2010+ writes it. §8.4 makes any "unsupported formula extension" `MANUAL_REVIEW`, yet also requires conditional-formatting references to be rewritten. Team 9 reads x14 conditional formulas as supported; this is already listed under `pending_confirmation` in suite 2.1.1. A team reading x14 as unsupported would classify the rename manual, refuse APPLY with 422, and fail `P4-APPLY-RENAME` without any rewrite defect. |
| Each reference is rewritten to exactly `'Data source'!…` (cell formulas, defined name, chart series, validation, x14 conditional, internal hyperlink) | §8.4 | No: the approved name contains a space, so quoting is required and the rest of each reference must be unchanged. |
| Ordinary text `Sheet1!B2`, the string literal `LEN("Sheet1")` and the similar name `'Sheet1 Notes'` stay unchanged | §8.4 | No. |
| `docProps/app.xml` may keep the old title or replace exactly that title; nothing else there changes | §11 comparator | **Possibly.** A target that regenerates extended properties (for example rewriting `HeadingPairs` counts) would be reported as an unexplained change. |
| The output audit no longer reports the renamed finding and uses the output hash in finding IDs | §8.2, §8.4 | No. |

## New diagnostic: single-reference rename variants

`tester/phase5/rename_probe.py` builds ten small workbooks from the same literal XML helpers as
the reviewed fixtures. Each has the same base (generic `Sheet1` plus `Report`) and exactly one
reference kind: none (control), unquoted formula, quoted formula, range formula, text/literal
controls, defined name, validation, x14 conditional formula, internal hyperlink, chart series.
The expected result for every variant is one `REQUIRES_USER_INPUT` WORKSHEET_NAME finding and a
rename whose only differences are that variant's rewritten reference; the x14 variant carries
the interpretation question above.

`python -m scripts.rename_probe --target-url <approved URL> --output diag.json` uses the tester's
own trusted access configuration, one fresh test namespace (cleaned afterwards) and normal
project calls. Per variant it records the classification, target and finding-ID agreement, the
APPLY/remediation outcome, the comparator problems and the output audit. It prints a summary of
classification, rewrite and refused-APPLY disagreements.

## Results so far

| Target | Run | Classification disagreements | Rewrite disagreements | APPLY refused |
|---|---|---|---|---|
| Team 9 system at this branch's base (`ca6becb`), private local API + worker (`--local-team9`) | 2026-11-12, `rename_probe_team9_local.json` | none (10/10 `REQUIRES_USER_INPUT`) | none | none |
| Team 12 test deployment | **Pending**: approved URL and private credentials not yet provisioned to the tester | – | – | – |

The Team 9 run shows the probe and the tester's comparator agree with Team 9's engine/editor on
every isolated reference kind; it is not evidence about Team 12.

## Next steps (joint)

1. Retrieve and retain the original failing report (bytes and SHA256) and record its message.
2. Run the probe against Team 12's test deployment. If only `conditional_x14` disagrees on
   classification, the cause is the §8.4 interpretation question, which needs a recorded pod
   decision before either team changes behavior or the shared oracle. If a rewrite variant
   disagrees, the comparator message names the part and reference.
3. Record the outcome and owner in ADOPTION.md; do not edit `expectations-phase4.json` or the
   suite until the pod agrees.
