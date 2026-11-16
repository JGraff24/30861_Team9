# Hosted Phase 5 own-system verification — November 16, 2026

Target: https://3.139.178.70.sslip.io/system
Tester: https://3.139.178.70.sslip.io/tester
Suite/specification 3.0.0, audit ruleset 2.0.0, 395 planned cases.

## Initial setup failure

The first authenticated hosted setup returned HTTP 500 with exactly
`{"message":"Tester report profile configuration is missing or invalid.","source":"tester"}`.
No session became runnable and no suite run POST was sent. The unchanged sanitized
[invocation record](setup-profile-failure-1/invocation.json) retains the actual response.
Connectivity and health succeeded; this was a tester configuration failure.

The same failure is reproduced locally with a missing trusted target-team mapping. The
production deployment previously declared the tester team but relied on a newly introduced
private mapping that could be absent in retained host configuration. Direct host inspection
was unavailable because AWS Core repeatedly accepted authentication without yielding an
API session; no AWS API result or account identity was obtained. Exact host image SHAs remain
unverified through the public interface.

## Configuration correction

Production now explicitly declares its own system URL using trusted `SERVER_NAME` through
`TESTER_OWN_TARGET_URL`. It must exactly match an already approved target and have the tester's
own team identity. Settings merge it into the private map while preserving cross-team entries;
conflicts and unknown/unapproved identities still fail. No grader request or host similarity
selects a profile. The packaged draft design map and independent test expectations are unchanged.

The private release probe now validates the own-team report profile/design map before opening
ingress. A regression showed that the former probe incorrectly accepted a missing design map.
This check creates no sessions or workbooks. The original failed setup is preserved separately
from the fresh post-release attempt; no rejected or uncertain POST is replayed.

Local correction checks passed: **105 affected tests**, full regression **1,121 passed /
7 skipped** in 200.84 seconds, lint/format, strict typing, schemas, Phase 5 policy/manifest
and coverage catalogs. The seven full-regression skips are five unconfigured hosted checks
and two Linux-only memory checks. The hosted manifest and independent expectations are unchanged.
[Local check record](profile-fix-local-checks.json) and [pytest XML](profile-fix-pytest.xml)
retain the fresh evidence; the two pre-fix reproduction logs retain the demonstrated failures.

Hosted suite execution and its outcome will be recorded here after deployment of the correction.
