# Worksheet-rename mismatch: engine-side analysis

Dylan (Team 9 engine), November 15 2026. Specification 3.0.0 §2 keeps the
worksheet-renaming mismatch reported against Team 12 open as a joint investigation and
forbids silently changing rules, finding identities or expected workbook results to erase
it. **This analysis changes no rule, engine behavior, expectation, finding identity, fixture
or suite case.** It complements Dixon's independent-test-side note,
`docs/evidence/phase5/rename_investigation.md` on `origin/phase5/dixon-tester` (f996909).

## Status

**Open. The available evidence does not show that Team 9's expectations are wrong.**
The most likely cause is a different reading of one §8.4 sentence, but that is not yet
proven. Resolving it needs evidence we do not have yet (listed under "Needed to close").

## Evidence actually available

| Evidence | What it shows | Limits |
|---|---|---|
| Phase 4 spec 2.1.0 §7.3, §8.4, §11 (`docs/contracts/phase4/`) | Rename must rewrite "conditional-formatting formulas" and lists "conditional-formatting references" among the *required supported* rename fixtures. Separately, any "unsupported formula extension" makes a rename `MANUAL_REVIEW`. The text does not say whether Excel's x14 conditional-formatting extension is a supported conditional-formatting formula or an unsupported formula extension. | Specification text only |
| `scripts.rename_inventory` on the shared fixture `rename_references.xlsx` (SHA256 `fabf363d…1d51`) → `dylan_rename_inventory_rename_references.json` | Team 9 classifies the rename `REQUIRES_USER_INPUT` with no blockers. Its eight references to `Sheet1` are three cell formulas, one defined name, one chart series, one list validation, one internal hyperlink and **one conditional formula, which exists only in the x14 extension** (`Sheet1!$B$2>5`). There is no main-namespace conditional formula. Applying `Data source` rewrites all eight, leaves no old-name reference and passes the engine's preservation verifier and output re-audit. | Team 9 engine only |
| Same tool, counterfactual field `strictX14Classification` | If an understood x14 conditional formula counted as an "unsupported formula extension", this required fixture's rename would become `MANUAL_REVIEW`. An APPLY would then return 422 `UNSUPPORTED_CORRECTION`, and `P4-APPLY-RENAME` would fail with no rewrite defect at all. | Hypothesis about Team 12, not an observation |
| Dixon's single-reference probe against Team 9 (`rename_probe_team9_local.json`, Dixon branch) | Team 9's engine and the independent comparator agree on all 10 isolated reference kinds, including x14 | Team 9 only; Team 12 probe pending |
| Independent comparator on Team 9 outputs (`excel_inspection/manifest.json`, `rename_references__rename` and `mixed_decisions__combined`) | `tester.phase4.package.compare_packages` finds exactly the approved rename differences in real system outputs | Team 9 only |
| Search of this repository's history and every fetched remote branch (November 15) | **No copy of Team 12's failing report, its session ID, the failing test ID(s) or the failure message exists anywhere in the repository.** The spec pack only states that the mismatch exists. | Absence of evidence |

## How Team 9's engine reads §8.4 today

`app/workbooks/cells.py` `_extension_conditional` treats an `x14:conditionalFormatting`
element as understood only when every rule is `expression` or `cellIs`, every formula
is a plain `xm:f` text node, and every attribute and child is from a known list. Its
formulas are rewritten by the same tokenizer as main-namespace conditional formulas.
Every other extension formula counts as an unsupported formula extension and blocks every
rename in the workbook. That includes unknown extension payloads, `INDIRECT`-constructed
references, x14 data validations and any other `f`/`formula` element in an extension. This
behavior is pinned by:

- `test_required_rename_fixture_supports_known_x14_conditional_formula`
- `test_opaque_or_constructed_x14_reference_extensions_remain_manual` (unknown `extLst`
  payload; `INDIRECT` inside `xm:f`)
- `test_known_x14_conditional_color_still_requires_review_after_safe_rename`

all in `tests/unit/test_dylan_phase4_review_regressions.py`. The integrated Phase 4
record (`docs/evidence/phase4_integration.md`) also states that known x14 conditional
formulas participate in safe renaming. Dixon's suite 2.1.1 lists the point under
`pending_confirmation`.

## Why the evidence does not justify changing Team 9

1. §8.4 requires the *supported* rename fixtures to include conditional-formatting
   references. In the shared fixture, the only conditional-formatting reference to the
   renamed sheet is in x14. Reading x14 as unsupported would make a required supported
   fixture unsupported. The two §8.4 sentences are only consistent if this kind of x14
   rule is the supported conditional-formatting representation.
2. Our understanding is that Excel 2010 and later store a conditional-format rule that
   references another worksheet in the x14 extension, not in the main
   `<conditionalFormatting>` element. If so, "conditional-formatting references" to a
   renamed sheet in real Excel files will normally be x14. **This is not yet confirmed
   for Excel 16.115.** See manual step R1 below.
3. The narrow understood profile keeps the §8.4 safety intent: anything unknown in an
   extension still makes renaming manual.
4. Nothing shows a defect in a Team 9 rewrite, comparator or finding target for this case.

Changing Team 9 to the stricter reading would change an expected workbook result. That
needs a recorded pod decision and spec change, which §2 and ADOPTION.md reserve for the pod.

## Consequences of each reading (for the pod decision)

| Reading | Team 9 engine | Shared fixture `rename_references` | `P4-APPLY-RENAME` against a team using the other reading |
|---|---|---|---|
| A. Known x14 `expression`/`cellIs` CF = supported CF formula (Team 9 today) | Unchanged | `REQUIRES_USER_INPUT`; all eight references rewritten | A team reading x14 as unsupported returns 422 and fails |
| B. Any x14 formula = unsupported formula extension | Would need a ruleset/spec change | `MANUAL_REVIEW`; fixture no longer covers a required supported reference kind; a main-namespace CF reference fixture would be needed | Team 9 would fail the revised expectation until changed |

Other possible causes, if Team 12's probe shows the x14 variant agrees:

- `docProps/app.xml` titles. Team 9 leaves `app.xml` byte-identical, and the comparator
  accepts either the old title or an exact replacement. A target that regenerates
  `HeadingPairs` would be flagged.
- Quoting. The approved name `Data source` contains a space, so every rewritten
  reference must be quoted (`'Data source'!…`). An unquoted rewrite is a real defect under §8.4.
- Over-broad replacement. `'Sheet1 Notes'`, the text cell `Sheet1!B2` and
  `LEN("Sheet1")` must stay unchanged.

## Needed to close (actions that need people)

1. **Retrieve Team 12's original failing report**: bytes, SHA256, test ID(s) and message.
   Store it unchanged next to this note. Without it, the cause stays a hypothesis.
2. **Run Dixon's probe against Team 12**
   (`python -m scripts.rename_probe --target-url <approved URL>` on Dixon's branch) once the
   private URL and credentials are provisioned. If only the x14 variant disagrees on
   classification, reading A vs B is confirmed as the cause.
3. **Manual step R1 (Dylan, Excel 16.115 on this Mac):** create a workbook with sheets `A`
   and `B`. On `B!A1`, add a conditional-format rule using the formula `=A!$A$1>5`. Save as
   .xlsx, unzip it and record whether the rule is stored as `x14:conditionalFormatting` /
   `xm:f` inside `<extLst>` or as a main `<conditionalFormatting>`. Record the Excel version,
   date and the relevant XML excerpt here. This tests premise 2 above.
4. **Record the pod decision** (reading A or B) with an owner in `ADOPTION.md`. Change
   rules, fixtures or `expectations-phase4.json` only after that decision.
