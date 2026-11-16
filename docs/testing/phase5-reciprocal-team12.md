# Phase 5 reciprocal testing: Team 9 tester against Team 12

Prepared by Dixon (Team 9), November 12 2026. Nothing here has been run against Team 12 yet.
Every value marked **pending** must come from Team 12 or the pod record (ADOPTION.md), never
from a guess.

## Inputs still needed

| Item | Status | Where it goes |
|---|---|---|
| Team 12 system base URL (no `/test` suffix, query, fragment or credentials; HTTPS) | **pending** | `ALLOWED_TARGET_URLS` |
| Private credentials: `userA`, `userB`, `reviewer` (read grant from userA only), `test-admin` | **pending** | `TARGET_AUTH_MAP_JSON[<url>]` (private env, never committed or printed) |
| Optional second test-admin `test-adminB` for the foreign-admin clock cases | **pending** (without it two cases skip) | same map |
| Team 12 team ID as Team 12 records it | **pending** | `TARGET_TEAM_MAP_JSON[<url>]` |
| Designated test deployment with clock and fault capabilities enabled | **pending confirmation** | Team 12 configuration |
| Team 12 target release SHA/tag for the run record | **pending** | `run_suite.py --target-revision` |
| Pod adoption of specification/pack 3.0.0 and this suite's manifest/fixture hashes | **pending** | ADOPTION.md |

## Tester configuration for a cross-team run

```
FIXTURE_MANIFEST_PATH=<wheel>/tester/fixtures/manifest-phase5.json   # or tests/fixtures/...
TESTER_TEAM_ID=team9
TARGET_TEAM_MAP_JSON={"<team12 url>": "<team12 id>"}   # exact URL; different team => cross
ALLOWED_TARGET_URLS=[..., "<team12 url>"]
TARGET_AUTH_MAP_JSON={"<team12 url>": {"userA": ..., "userB": ..., "reviewer": ..., "test-admin": ...}}
```

The profile is decided only by this trusted configuration and is frozen with the session at
setup: a cross-team report omits `coverage.model` entirely, and the tester never needs Team 12's
design map. The instructor setup body stays exactly `{"target_url": ...}`.

The hosted tester proxy now allows 2,460 s (`deploy/nginx.conf.template`), the Phase 5 plan
carries the 2,400 s suite budget, and `scripts/run_suite.py` reads the 2,430 s caller timeout
from the manifest. Switching the deployed tester from the Phase 4 to the Phase 5 manifest
(`deploy/compose.production.yaml`, `FIXTURE_MANIFEST_PATH`) is a release step to coordinate
with the deployment owner; the deploy probe already accepts either suite.

## Running it

1. Pre-flight (no suite run): `python -m scripts.rename_probe --target-url <url> --output
   rename-team12.json` gathers the isolated rename evidence for the open investigation
   (docs/evidence/phase5/rename_investigation.md) through one cleaned test namespace.
2. Full run, once per session:

   ```
   TEAM9_TESTER_TOKEN=... uv run python -m scripts.run_suite \
     --tester-url <team9 tester url> --target-url <team12 url> \
     --target-revision <team12 sha/tag> --manifest tests/fixtures/manifest-phase5.json \
     --output report.json --record run-record.json
   ```

   Or start the `ci` workflow by hand with `suite=phase5`, `expect_profile=cross`.
3. Retain `report.json` (unchanged bytes) and `run-record.json` (counts, hashes, profile,
   per-operation and per-AC status, `classification: unclassified` for each non-pass). Triage
   each non-pass as system, test or specification and record the owner; never relabel the
   stored report.

## What to expect and how to read it

- `fail` is an evaluable disagreement; `error`/`system` is an unexpected target internal
  failure (for example an accepted clean fixture ending `PROCESSING_FAILED`);
  `error`/`communication` is no response; `skip` never ran and says why.
- Busy-version, active-deletion and PROCESSING-eligibility cases assert only when the active
  window is observed; a fast target may legitimately produce skips there.
- `P5-CLOCK-NOT-READY` and `P5-FAULT-REJECT-DISABLED` are unobservable over HTTP on a
  deployment with the capabilities enabled and report skips.
- Fault and clock isolation probes upload one clean workbook into Team 12's ordinary data as
  userA and remove it with owner deletion. Tell Team 12 before the run.
- Each scenario that moves simulated time uses its own fresh namespace; the tester records them
  and cleans them through `cleanup_session_ids` on its next setup for that target.
