# Hosted Phase 4 failures: November 1, 2026

Target contract: specification **2.1.0**, ruleset **2.0.0**, context pack/suite **2.1.1**.
All eight checked-in context-pack files match the supplied revision 2.1.1 ZIP byte for byte.
This investigation does not change rule identities, historical audits or retained reports.

The supplied Team 12 report contains 215 cases: 96 pass, 6 fail, 13 skip and 100
communication errors. Its SHA-256 is
`3ad0eedcb17c8f00d0476251be519c643aa93db768a1bf11eaf82b266e91300f`.
Host inspection used read-only SQLite queries and system journal reads through AWS Systems
Manager. No host settings, services, data or deployment were changed for this investigation.

## Queue timeouts

The host retained a 250,000-cell boundary audit claimed at 18:47:53.255988 UTC and
completed at 18:48:51.375988 UTC: 58.12 seconds occupying the sole execution slot.
Both teams' sessions submitted small audits during that interval. Their failed job records
have no worker lease and completion times just after their acceptance-plus-15-second queue
deadlines. These were queue failures, not 60-second engine failures. The two reported
`P4-REAUDIT-INVALID` failures occurred while auditing their clean preparatory uploads,
before submitting either invalid re-audit body.

CloudWatch's five-minute CPU averages were approximately 72.5% at 18:45 UTC and 76.4%
at 18:50 UTC. The instance was in unlimited CPU-credit mode with more than 550 credits
remaining, so exhausted burst credits do not explain this incident. This is not a benchmark
of the revised two-slot worker on the EC2 host; that deployment/load check remains pending.

The production worker now has two bounded execution slots. Each slot claims only when it
can immediately execute; it never claims work into an internal waiting queue. Cleanup has
its own housekeeping loop, and signals stop new claims while allowing bounded running work
to finish. Future recovery errors distinguish `queue` from `worker` in `details.stage` and
produce correlated expiry logs. No queue or execution deadline is extended.

The independent tester also drains schema-valid accepted work between cases, including an
incorrect 202 response to a negative request. That original negative assertion remains a
failure. This prevents a separately reproduced cascade; it is not the explanation assigned
to the two hosted failures. Failed fixtures are never replaced with a fresh successful audit.
Team 12 should likewise finish accepted boundary work before submitting dependent cases.
Two slots are bounded capacity, not evidence that unlimited simultaneous suites fit this host.

## Connection failures during deployment

The audit being polled by the first communication-error case, `P4-APPLY-NO-EDITS`,
completed successfully at **19:00:22.350052 UTC**. The deployment journal then records
nginx stopping at **19:00:23 UTC** and restarting at **19:00:41 UTC** for the guide-only
release `0da3c790edd6eb4aa8ab1c689897e3bf99d8db88`. The old deployment guard counted
active jobs and local tester runs; it missed external sessions between requests.

Authenticated isolated-session setup now persists a 1,900-second deployment protection
lease. Allowed business requests renew it, with writes limited to roughly once per 100
seconds. The deployment guard counts unexpired PREPARING/READY sessions before builds
and activation. Old payloads use setup time plus 1,900 seconds without being rewritten.
Cleaned/failed sessions release protection. Instructor coverage/report content remains
read-only and byte-stable. No SQL schema-version migration is required.

The report supplies only the exception class `ConnectError`, without per-request timestamps
or underlying connection reasons. The deployment interruption explains an availability gap
at the first failing operation; attribution of every later communication error remains
unverified. A fresh remote run against the released fix is still required. Do not treat those
100 errors as 100 demonstrated rule or remediation defects.

## Formula-link target discrepancy

Team 12 expects `#cell=A11`, `#cell=A12`, `#cell=A13` for formula links. Our engine and
independent fixtures use indexed XML paths ending in `/row[11]/c[1]`, `/row[12]/c[1]`,
`/row[13]/c[1]` in the actual worksheet part. Classification remains MANUAL_REVIEW.

[Section 7.6](../contracts/phase4/Phase4_Shared_Specification.md) says: “Supported simple
text-cell links use the cell target; unsupported representations use indexed XML targets.”
It expressly treats computed arguments as unsupported and disallows formula corrections.
The computed-link `#cell=A13` expectation conflicts with that unsupported-target policy.
The wording leaves room for disagreement about whether a screened literal formula qualifies
as a simple text-cell link, although our implementation consistently treats formula links as
unsupported correction representations. Request a shared clarification for literal formulas
before changing either oracle. Retargeting would also change finding IDs under the same
ruleset, so this patch does not silently change production targets or either fixture oracle.

The corrected HSL tint expectations remain `336699/-0.5 → 1A334D` and
`336699/0.5 → 8CB3D9`, with the pack's additional half-boundary regressions retained.
This discrepancy concerns hyperlink targets, not tint calculations.
