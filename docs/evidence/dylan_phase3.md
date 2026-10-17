# Dylan Phase 3 evidence index

Dated 2026-10-14. Branch `feature/phase3-audit` from base `ffa6947`. The work is local and
uncommitted at the time of writing (no commit SHA or tag yet). Everything below was actually
run on Dylan's machine unless it is marked **PENDING**.

## Mapping to the Phase 3 assignment questions

| Question area | Dylan's contribution | Evidence |
|---|---|---|
| Q1 Delivered scope vs milestone | Workbook validation and audit engine (`OoxmlWorkbookEngine`) with both Phase 3 rules; no remediation | [implementation doc](../implementation/dylan_phase3.md) "Status" and "Non-capabilities" |
| Q2 Release, hosting, startup/logs | None from Dylan: the engine is a library. Hosting and assembly are Jackson's | PENDING (Jackson) |
| Q3 Status of the 14 operations | Supplies the engine behind operation 2 (upload validation) and 5 (audit results). Operation status itself is consolidated by Jackson | PENDING (Jackson) |
| Q4 Test-ID-to-AC map, independent oracle | AC table in the implementation doc. Oracles: hand-written `expectations.json`, golden hash literals from a standalone stdlib one-liner, expected targets written from seeds | `tests/unit/test_*`, `tests/fixtures/synthetic/dylan/expectations.json` |
| Q5 Regression and static checks | 345 tests passed; ruff, ruff format and strict mypy pass; foundation scripts pass | "Run log" below |
| Q6 Deployed run results | None | PENDING (no deployment exists) |
| Q7 Models and consistency review | Actual Mermaid models in the implementation doc, written after the code and checked against it | Ryan's model-to-code review PENDING |
| Q8 Cross-testing both directions | None | PENDING (Team 12 fixtures and URLs not available) |
| Q9 Limitations and Phase 4 risk | L1–L10 table; preservation experiment results | Implementation doc; [experiments/preservation/README.md](../../experiments/preservation/README.md) |

## Run log (2026-10-14, macOS 24.5 arm64, Python 3.12.3, uv 0.13.1)

```text
uv sync --locked --group dev                         ok (after freeing disk space)
uv run pytest tests/unit tests/contract              345 passed
  of which Dylan's 7 files                           204 passed
uv run ruff check .                                  All checks passed!
uv run ruff format --check .                         50 files already formatted
uv run mypy app tester                               Success: no issues found in 29 source files
uv run python -m scripts.export_schemas --check      ok (system + tester)
uv run python -m scripts.check_identity              ok
uv run python -m scripts.smoke_foundation            ok (paths empty; pending routes 404)
uv run python -m experiments.preservation.run_experiment   results/2026-10-14.json
```

Synthetic fixture SHA256 values (`tests/fixtures/synthetic/dylan`):

| File | SHA256 |
|---|---|
| clean.xlsx | fe2d24918ab4f56ef4e41acae3a7a2e23f06d1fefd8d717b642fea8d3c2a3ae9 |
| seeded_issues.xlsx | 3a2b7e623f0090fcf315e6c4476c200a868cd48f923b339ba48944cc78bdc8b4 |
| edge_cases.xlsx | 18e00f2040dd48c29edc054ccde4c1d9a5e2191416532c94916ee9a71d52f304 |
| corrupt.xlsx | c09fe23b535fc60f7c9ca826321c2c943fd88af1ac929e6969af9cd0ab6486ff |

## Pending items, with owners

- Team 12 shared fixtures and an independently reviewed manifest: Team 12 / Dixon.
- Two-member manual review of `expectations.json`: Dylan plus one teammate.
- Opening the preservation outputs in Excel without repair warnings: Jackson / Dylan.
- HTTP and worker integration of `OoxmlWorkbookEngine`: Jackson.
- Coverage measurement of engine invocations: Ryan.
- Five de-identified real-world workbooks, the Excel Accessibility Checker comparison and
  90% precision: pod (human inputs required).

## GenAI use (actual)

- **Tool:** Claude Code (model Claude Opus 5.5) in the Claude desktop app. The handoff's
  starter prompt targeted Codex; Dylan used Claude Code instead.
- **Delegated:** reading the handoff and bootstrap code, creating the branch, implementing
  `app/workbooks` and `app/audit`, writing the test builder, tests, fixtures, preservation
  experiment and these documents.
- **Verification performed:** full test, lint and type runs; golden digests checked against
  the handoff value; deliberate code mutations to confirm the tests catch bugs; expectations
  written from seeds before the fixture tests were run.
- **Real corrections during the session:**
  1. The file-writing step decoded `\u` escapes in strings into literal invisible characters
     (for example U+3000 and U+200B) in test files. This was caught by a scan for non-ASCII
     and control characters, and the characters were converted back to explicit escapes so
     reviewers can see them.
  2. A malformed-XML test used a payload with the wrong root, so the engine (correctly)
     reported `sheet_root` instead of `malformed_xml`. The test payload was fixed; the engine
     was not changed.
  3. A whitespace test included U+000B/U+000C. These are illegal in XML 1.0, so the engine
     rejected the workbook. The case was moved into a separate test documenting that behavior.
  4. The first preservation comparison wrongly reported objects as "missing" after a
     targeted sheet rename, because objects were grouped by sheet name. The comparator now
     remaps names through the rename before diffing.
- **Agentic engineering reflection:** PENDING. Dylan writes this personally.
