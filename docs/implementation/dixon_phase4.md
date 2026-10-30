# Dixon Phase 4 implementation: independent tester and CI

Branch `phase4/dixon-tester`, based on `main` at `d72d9a2`. Target contract: Jackson's shared
specification pack 2.1.0 (ruleset 2.0.0, manifest 2.1.0), committed unchanged under
`docs/contracts/phase4/`. This branch changes the tester, its fixtures, CI and the tester's
proxy timeout. It does not change the system's API, engine, editor, storage or UI.

## Delivered boundary

| Area | Files | What it does |
|---|---|---|
| Wire contract | `tester/phase4/contract.py` | Validates bodies against the frozen JSON Schema 2020-12 pack (44 definitions), strict UTF-8 JSON (no duplicate keys or NaN), strict UTC RFC 3339 timestamps |
| Policy oracle | `tester/phase4/policy.py`, `scripts/check_phase4_policy.py` | The tester's own reading of sections 6-7: White_Space, generic names, link labels, contrast, HSL tint, components. Checked against every frozen vector and all 18 examples |
| Manifest | `scripts/build_phase4_manifest.py`, `tests/fixtures/manifest-phase4.json` | All 215 hosted plan entries bound to 73 handlers. Catalogs C1-C9, AC-01 to AC-11, operations 1-14 plus S/V/R, section 10 limits |
| Checks | `tester/phase4/checks.py` | 36 Phase 4 handlers (fault session, versions, re-audits, review context, decisions and rejections, idempotency, downloads, reports, history, access matrix, sessions); Phase 3 handlers run the retained `P3-*` cases |
| Comparator | `tester/phase4/package.py` | Independent package comparison for section 11 (details below) |
| Fixtures | `tester/phase4/workbooks.py`, `tester/phase4/vectors.py`, `tests/fixtures/expectations-phase4.json` | 16 Team 9 synthetic packs plus re-derived Phase 3 fixtures; see `tests/fixtures/README.md` |
| Shared modules | `tester/suite/{manifest,runner,records,checks,registry}.py`, `tester/service.py`, `tester/store.py`, `tester/target.py` | Versioned manifests, Phase 4 coverage rules, per-request response bounds, auxiliary-session cleanup |
| Operator/CI | `scripts/run_suite.py`, `scripts/tester_selfcheck.py`, `.github/workflows/ci.yml`, `deploy/nginx.conf.template`, `scripts/deploy_host.py` | `--manifest`/`--read-timeout`, `--phase4` self-check, new CI steps, 1,900 s tester proxy timeout, and autodeploy waits up to 1,900 s for active runs |

The Phase 3 manifest stays the default (`FIXTURE_MANIFEST_PATH`), so the deployed Phase 3
workflow is unchanged. A Phase 4 deployment sets `FIXTURE_MANIFEST_PATH` to
`tester/fixtures/manifest-phase4.json` (wheel) or `tests/fixtures/manifest-phase4.json`.
With a Phase 4 plan, the tester's run deadline comes from the plan (1,800 s); callers use at
least 1,830 s (`run_suite.py` reads it from `--manifest`).

## Independence

The tester never imports the system's engine, editor, storage or preservation comparator.
- **Wire shapes:** the adopted JSON Schema pack, validated by `jsonschema`, which is now a runtime dependency.
- **Expected findings:** hand-written expectations. Finding IDs come from the tester's own
  `identity.py`.
- **Output workbooks:** checked by the tester's own comparator.
- **The fake target:** `tests/support/fake_phase4.py` exists only to test the tester and
  never ships.

## Comparator rules (section 11)

- Every entry not named by an approved effect must have byte-identical decompressed content.
  The entry sets must match, except for parts an approved header-range effect adds. ZIP
  directory entries are ignored.
- **Changed XML parts** are parsed safely, with DTDs and entities forbidden and comments kept.
  Each effect (`alt`, `link`, `rename`, `header_range`, `header_table`):
  1. proves its positive result, for example the exact title/description pair, an inline-string label, every rewritten reference, or the new table part, relationship and content type;
  2. then masks only what it may change.
  Everything left must compare equal, element by element.
- **Namespaces** are compared by URI. A dropped declaration, or an undeclared prefix inside an
  `mc:` attribute, is a problem; a consistent re-prefixing is not.
- **Tolerated equivalents:** `s="0"` equals an omitted cell style; `xml:space` without edge
  whitespace is ignored; header metadata and an autoFilter may be added when a table header is
  enabled; `docProps/app.xml` sheet titles may follow a rename.
- **Malformed or undecodable output** is a check failure, never a tester abort.

## Outcomes and coverage (sections 14, 16)

- `fail` means an evaluable assertion disagreed. A `PROCESSING_FAILED` audit of an accepted
  fixture is a failure, not an error (section 1).
- `skip` means a real prerequisite was missing, with the reason stated. A shared upload, audit
  or remediation that failed is reported once; its dependents then skip fast instead of
  re-uploading.
- **Phase 4 tester errors:** an unexpected internal tester problem in one Phase 4 case becomes
  that case's `error` (source `tester`), and the run continues. Phase 3 keeps its documented
  abort behaviour.
- **What earns coverage:** shared setup calls never record markers, and skipped cases earn no
  coverage in either phase. C9 is never counted by an HTTP-only run, so component coverage is
  at most 8/9. System coverage is exercised-criterion coverage over AC-01 to AC-11, not
  acceptance.
- **Running out of suite budget** while polling is the tester's own deadline, not a target
  failure.

## Fault session and cleanup (section 14.1)

- **Fault cases:** `P4-FAULT-SETUP` creates a fresh namespace with `faults.audit_failures = 1`.
  A 403 there is a failed capability check. `P3-AUDIT-FAILURE-ENVELOPE` uploads
  `clean_all_six` into that namespace and verifies:
  - the exact `TEST_AUDIT_FAILURE` envelope;
  - the failed report (no checks performed);
  - unchanged source bytes;
  - the version's latest audit.

  `P4-FAULT-RECOVERY` re-audits normally, then checks that history lists the failed audit
  followed by the recovered one.
- **Evidence retention:** the fault session's ID appears in the case message. Every auxiliary
  namespace the run creates is recorded in `tester_auxiliary_sessions` and cleaned through
  `cleanup_session_ids` on the next setup for that target, never in the middle of the run.
  `P4-SESSION-CLEANUP` cleans only its own scratch namespace.

## Verification loop (this branch)

1. **Blind fixture oracle.** A separate agent derived the findings for every accepted fixture
   from the raw XML and the specification, without seeing these expectations. It agreed on 25
   of 26 workbooks, including all four rule-vector packs and Team 12's file (6 findings). The
   one difference is `form_controls`' Fallback OLE copy, which pod contract 1.1 settles.
2. **Spec-conformance review**, which found:
   - coverage earned by skipped cases and setup calls;
   - weak rule-vector checks;
   - a stale-audit race;
   - fault-evidence retention;
   - comparator strictness.

   All were fixed. The judgment calls it raised are listed under `pending_confirmation` in the manifest.
3. **Correctness review**, which found:
   - two suite-abort paths (a corrupt output ZIP, and coverage with `covered > total`);
   - string timestamp comparison;
   - retry storms on shared failures;
   - polling-budget handling;
   - the 660 s proxy timeout.

   All were fixed, with regression tests.
4. **OOXML validity review.** It found that the cross-sheet conditional format belongs in the
   x14 extension and that the comment VML was too thin. Shapes were also invisible. All were
   fixed, and the reviewer judged the fixtures and corrected outputs otherwise Excel-safe. This
   is a reading, not an Excel run.
5. **Final fresh re-review** confirmed the earlier fixes and found further problems, all
   fixed with regression tests:
   - a shared-result ordering bug that turned one preservation defect into 19 false skips;
   - comparator false passes on a misplaced `tableParts` and on wrong `count` values;
   - `mc:` prefixes resolved outside their scope;
   - pathological JSON raising instead of failing;
   - auxiliary-session IDs saved only at the end of the run;
   - a competing re-audit ID recorded too late.

   Two low-severity items remain open:
   - `P4-SESSION-COVERAGE` runs at plan position 210, so code coverage misses the final few
     calls.
   - The `xml:space` tolerance looks only at the element's own text.
6. **Tests:**
   - `tests/unit/test_phase4_tester.py`: contract, policy, manifest binding, expectation
     consistency, the comparator (accepts every plan, rejects 12 mutations, tolerates
     re-prefixing), a full conforming run (215/215), six defective targets, a corrupt output,
     inconsistent coverage, a failed shared audit, fractional timestamps, deferred cleanup and
     coverage accounting.
   - `scripts.tester_selfcheck --phase4` repeats the full run over real sockets.

## Pending (not done, not claimed)

- Two-member Team 12 review of every Phase 4 expectation, and Excel no-repair checks of each
  fixture and corrected output.
- Pod agreement on the `pending_confirmation` items in the manifest, and on expanded IDs for
  grouped negative vectors before the count is frozen.
- A real Team 9 / Team 12 system implementing Phase 4. Every run so far used the test-only fake.
- Local-integration (24) and manual-release (10) plan entries. They are outside the hosted
  report; the system owners exercise them.
- C9 review-UI evidence. This needs a browser or manual check and is outside HTTP runs.
