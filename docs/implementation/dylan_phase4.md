# Dylan — Phase 4 engine: six rules, review context, corrections and preservation

Target: specification 2.1.0, ruleset 2.0.0. Built on Jackson's `codex/jackson-phase4-backend`
branch and its `CorrectionEngine` port. This is an engine implementation, **not completed
Phase 4 acceptance**: the Excel no-repair inspections, the other team's answer-key review and
the reciprocal hosted runs are still outstanding (see the end of this note).

## What the engine now does

`app.audit.engine.OoxmlWorkbookEngine` declares `supported_rulesets = ("1.0.0", "2.0.0")`.
Ruleset 1.0.0 keeps the Phase 3 two-rule behavior unchanged for regression and historical
audits. Ruleset 2.0.0 runs all six rules and returns `checks_performed` in contract order plus
a `review_context` item for every finding, in finding order.

| Rule | Detection (spec §7) | Correction (spec §8) |
|---|---|---|
| ALT_TEXT | Phase 3 inventory, unchanged targets and duplicate-ID paths | Sets `cNvPr` `title` and `descr` on uniquely identified DrawingML objects |
| WORKSHEET_NAME | Generic `SheetN` names, chartsheets and hidden sheets included | Renames the sheet and rewrites references in cell formulas, defined names, chart series, data validation, conditional formatting and internal hyperlink locations |
| TABLE_HEADER | Formal tables (disabled/blank/duplicate/formula/merged/overlapping/mismatched headers, duplicate IDs) and unstructured 4-neighbor components of at least 2×2 | Creates a minimal table over a confirmed dense candidate, or turns on a disabled header whose first row already matches the column names |
| MERGED_CELLS | Populated multi-cell merges; overlapping merges with a limitation | None (always manual) |
| LINK_TEXT | Element hyperlinks and `HYPERLINK` formulas against the finite vocabulary, raw addresses and destinations | Replaces the label of a simple single-cell text link with inline text; shared strings are never edited |
| COLOR_CONTRAST | W3C contrast and color-only screening per populated cell, plus conditional-format regions | None (always manual) |

A finding is `REQUIRES_USER_INPUT` only when the editor can apply its correction safely;
anything else is `MANUAL_REVIEW` with a limitation explaining why. Signed packages, shared
drawing or table parts, non-UTF-8 parts and every rename-unsafe structure listed in §8.4
(3D/external references, INDIRECT/EVALUATE, HYPERLINK formulas, shared/array/dynamic
formulas, pivots, slicers, connections, form-control formulas, extension formulas, embedded
workbooks) downgrade the correction to manual review.

## Code map

| Module | Responsibility |
|---|---|
| `app/workbooks/inventory.py` | One immutable source inventory shared by the audit, preflight, editor and verifiers |
| `app/workbooks/cells.py` | Streaming worksheet model: coordinates, resolved text, styles, merges, links, conditional formats, validations, tables |
| `app/workbooks/styles.py` | Styles, workbook theme, indexed palette and exact-rational tint |
| `app/workbooks/formulas.py` | Sheet-reference tokenizer, rename rewriter and literal `HYPERLINK` parser (never evaluates) |
| `app/workbooks/xmltext.py` | Lexical span editor: changes only the exact characters a correction needs |
| `app/workbooks/editor.py` | Applies corrections and rebuilds the ZIP in source order with source metadata |
| `app/workbooks/preservation.py` | Independent tree-based comparison against the explicit allowlist |
| `app/audit/rules_v2.py`, `color.py`, `review.py`, `objects.py` | Six-rule analysis, contrast math, review context, shared object inventory |

Editor and verifier code sits under `app/workbooks` and `app/audit`, so Ryan's measured
coverage scope already includes it.

### Preservation design

The editor never loads and re-saves a workbook. It locates elements by indexed path or
unique ID and replaces character spans, so namespace prefixes, `mc:Ignorable`, unknown
markup, comments, attribute order and XML declarations survive byte for byte. Untouched
ZIP entries keep identical uncompressed bytes, order, timestamps and compression type.

`verify_preservation` does not trust the editor. It re-parses both packages with the
hardened tree parser, rebuilds the expected tree from the source plus the approved changes,
and requires exact equality. It also requires that no entries were removed or reordered,
that only the expected parts were added, and that no namespace declaration changed except a
local `r` prefix on a new `tablePart`. `verify_corrections` re-reads the output with a fresh
inventory and checks each approved effect plus the absence of the matching output finding.

## Decisions that need Team 12 agreement

1. **Tint rounding (§7.7 vs the policy vectors).** The engine follows the normative text:
   exact rational HSL arithmetic, halves rounded up. That gives `1A334D` and `8CB3D9` for
   base `336699` at tint −0.5/+0.5, where `phase4-policy-vectors.json` lists `19334D` and
   `8CB2D9` (floating-point artifacts, as Jackson noted). Proposal: correct the two vectors.
2. **LINK_TEXT targets.** Supported simple cell links use `#cell=A1`. Every unsupported
   representation uses an indexed XML target: range, duplicate, rich-text, formula-cell and
   malformed element links target their `<hyperlink>` element; `HYPERLINK` formulas target
   their `<c>` element.
3. **`currentValue`** is provided only for `REQUIRES_USER_INPUT` items, and is null for
   manual items.
4. **Display-assumption disclosure.** Missing fill/font color is screened as white/black and
   disclosed once per audit, so almost every audit carries that limitation (status is not
   affected).
5. **Noisy-but-normative cases.** `auto` font colors, indexed 64/65, patterned fills, mixed
   rich text and table-style overlays produce one manual finding per populated cell, as §7.7
   requires.
6. **Rename safety is workbook-wide.** Any rename blocker makes every rename in that
   workbook manual, because §8.4 requires the complete reference inventory to be understood.
7. **New tables** have `headerRowCount="1"`, `totalsRowShown="0"`, and no `autoFilter` or
   `tableStyleInfo` (§8.6 "no filter, no style overlay"). `docProps/app.xml` sheet titles
   are left byte-identical after a rename; Excel regenerates them on save.

## Verification

| Check | Result |
|---|---|
| `pytest tests/unit/test_dylan_phase4_rules.py tests/unit/test_dylan_phase4_corrections.py tests/unit/test_dylan_phase4_http.py` | 95 passed |
| Full `pytest -q` | Same 23 failures as Jackson's branch before this work (superseded Phase 3 assertions and the 2.0.0 test-double configuration he documented); no new failures |
| `ruff check .`, `ruff format --check .`, `mypy app tester scripts` | Passed |
| `scripts.export_schemas --check`, `check_identity`, `make_synthetic_fixtures --check` | Passed (`smoke_foundation` fails on the same route-count assertion as before) |
| 250,000-cell worksheet at the intake limit | Audit 9.2 s, peak RSS 276 MiB (limits 60 s / 512 MiB) |
| openpyxl load of corrected outputs | Tables, labels, destinations and renamed references read back correctly (not a substitute for Excel) |

The HTTP test drives the production engine through the real API, SQLite, filesystem storage
and isolated worker children: upload, audit, review, mixed APPLY/DECLINE remediation, output
download, output audit and report. Policy-vector tests cover identity, generic names,
ambiguous labels, contrast, tint, header components and duplicate-ID targets.

Jackson's `test_missing_editor_is_explicit_dependency_failure` asserted the real engine had
no editor. It now uses an audit-only engine double, so it still checks the 503 path.

## Outstanding before Phase 4 acceptance

- Open each supported correction fixture's output in Microsoft Excel and record no repair
  prompt (§11 release gate): new table, enabled header, link label, alt text, rename with
  every reference type.
- Two Team 12 members review the fixture expectations; agree on items 1–7 above and record
  them in the shared contract.
- Dixon's independent all-six fixture pack and reciprocal hosted runs.
- Ordinary deletion, scheduling and notifications stay in Phase 5.
