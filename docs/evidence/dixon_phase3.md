# Dixon Phase 3 evidence

Owner: Dixon (tester, tests/contract, tests/acceptance, fixture manifests, CI).
Branch `phase3/dixon` from foundation `ffa6947`. Implementation details:
[../implementation/dixon_phase3.md](../implementation/dixon_phase3.md). Every entry below is an
actual local run unless it is marked **pending**.

## Test run log

| Date (local) | What ran | Against | Result |
|---|---|---|---|
| 2026-10-14 | Baseline `pytest tests/unit tests/contract` on fresh checkout | Foundation only | 139 passed, 2 failed (Windows-only: cp1252 profile read; socketpair blocked by the no-network guard) |
| 2026-10-14 | Same, after the Windows fixes (commit `18c687c`) | Foundation only | 141 passed |
| 2026-10-14 | `pytest tests/unit tests/contract` | Tester, test-only fake transport | 189 passed |
| 2026-10-14 | `scripts.tester_selfcheck`, run 1 | Real tester process → **test-only fake target** process | HTTP 200 in 11.5 s; 22 run / 22 pass / 0 fail / 0 error / 0 skip; code 40/50 (fake value), component 6/6, system 8/8; acceptance 3 passed |
| 2026-10-14 | `scripts.tester_selfcheck`, run 2 | Same | HTTP 200 in 10.5 s; 22/22 pass; acceptance 3 passed |
| 2026-10-14 | pip-audit, locked runtime + measurement extra | Lock file | No known vulnerabilities found |
| **pending** | `scripts/run_suite.py` | Integrated local Team 9 system | System endpoints not implemented yet |
| **pending** | Same | Team 9 on AWS | No deployment or URL |
| **pending** | Team 12 tester → Team 9 system, and Team 9 tester → Team 12 system | Cross-team | URLs and access provisioning not available |
| **pending** | GitHub Actions `ci` | Pushed branch | Branch not pushed |

The fake target's 40/50 code coverage is a constant in `tests/support/fake_system.py`; it is
not a measurement of any real code. Self-check results show the tester's own behavior and
nothing about the Team 9 system.

## Cross-team report coverage

**Pending.** No cross-team report exists. When one does, record for each direction: run
datetime, tested tag/SHA, target URL, run/pass/fail/error/skip counts, each failure classified as
system, test or specification, and code/component/system coverage with their bases. The component
and system catalogs (C1-C6, eight AC portions) must be shared with Team 12 before quoting
percentages.

## Evidence index (contributions → assignment areas)

| Area | Evidence | Status |
|---|---|---|
| Delivered scope | Implementation doc "Delivered boundary" | Local only |
| Test-ID to AC map with oracle rationale | Implementation doc "Suite and traceability"; `tests/fixtures/manifest.json`; `tests/fixtures/README.md` | Done; manual review pending |
| Regression and static checks, CI | Commands table in the implementation doc; `.github/workflows/ci.yml` | Local passes recorded; CI link pending a push |
| Deployed-run results | This log | Pending integration and deployment |
| Models and consistency | Implementation doc Mermaid models (checked against `tester/` code) | Done |
| Cross-testing and ambiguities | Contract confirmation items below | Pending runs |
| Limitations and Phase 4 risks | Implementation doc limitations table | Done |

### Ambiguities found while implementing (for Jackson's Team 12 confirmation list)

- Exact error `code` strings for 400/401/403/404/413/415/422 (only the `.xlsm` code is fixed).
- Whether `X-Test-Session` should go on the public health call (the tester sends it; an unknown
  session there would be a 400 under the current default).
- How a tester should clean up namespaces after a failed setup (implemented as opt-in
  `cleanup_session_ids` on the next setup).
- Whether an unreferenced-ancillary-chunk PNG padding approach is an acceptable way to hit the
  exact 25,000,000-byte boundary for both teams' validators.

## GenAI use (Claude Code, this session)

**Delegated:** reading the repository and contract; designing and writing the tester package,
suite, fixtures generator, tests, CI workflow and these docs; running all verification commands.
Dixon directed the scope (implement the owner part, clone into `C:\Users\Dixon Wagner\code`, no
push). Dixon has not yet reviewed the resulting code.

**Verification performed:** every claim in the results tables came from a command executed in this
session (outputs are quoted in the implementation doc). Real processes on 8000/8001 were used for
the self-check. Tests cover the failure paths, not just the passing path.

**Real corrections that occurred:**

1. The first diagnosis of the baseline profile-hash failure blamed CRLF line endings, and a
   `.gitattributes` was added. A re-run showed the same digest. The actual cause was
   `Path.read_text()` defaulting to cp1252 on Windows. The `.gitattributes` was removed and
   `encoding="utf-8"` added.
2. The deadline-abort test passed or failed depending on timing, because Windows `time.monotonic()`
   ticks roughly every 15 ms, so a 1 µs deadline didn't always expire. The fix injected the
   clock into the target client and advanced it deterministically in the test.
3. The fixtures were first DEFLATE-compressed. That output depends on the zlib build, so the
   committed bytes could differ between Windows and Linux CI. They were switched to stored
   entries and a fixed PNG `IDAT` stream.

**Reflection (Dixon to write):** _Personal reflection on agentic engineering is the student's own
and has not been drafted here._
