# Automatic updates on the existing AWS host

The EC2 host checks public GitHub `main` every two minutes. When its SHA differs from
the deployed SHA, it requires successful **push CI for that exact commit** before updating.
Failed/pending CI, pull requests and feature branches cannot deploy.

This uses the existing EC2 server and HTTPS egress. It does not require another AWS role,
OIDC provider, AWS access key, GitHub token, SSH port or container registry. The initial
OIDC design was replaced after this AWS project's service control policy explicitly denied
IAM OIDC-provider access. No change to that policy is needed.

Installation and the first real automatic release still require verification on the host.

Local verification on October 18, 2026: **555 passed, 6 skipped**, including 27 deployment
checks. Ruff lint/format, strict mypy, schema freshness and installer shell syntax passed.
Both Python deployment scripts were checked for Python 3.9 syntax. The six skips were four
opt-in live acceptance checks and two Linux-only memory enforcement checks on macOS.

## One-time installation

After the reviewed changes are merged and CI passes, open **EC2 → Instances →
i-0922ebb45b0bed415 → Connect → SSM Session Manager**. Run the commands there.
Use the full reviewed commit SHA, not the literal placeholder:

```sh
TEAM9_BOOTSTRAP_SHA=FULL_REVIEWED_COMMIT_SHA
curl --fail --location --max-time 60 \
  "https://raw.githubusercontent.com/JGraff24/30861_Team9/$TEAM9_BOOTSTRAP_SHA/deploy/install_autodeploy.sh" \
  -o /tmp/team9-install-autodeploy.sh
sudo bash /tmp/team9-install-autodeploy.sh "$TEAM9_BOOTSTRAP_SHA" JGraff24/30861_Team9
```

The installer requires the existing `/data` EBS mount, running Docker/Compose, Git,
curl, Python 3.9+, trusted public HTTPS, existing SQLite stores, and the initial private
configuration at `/opt/team9/config/compose.env` and `/opt/team9/config/runtime.env`.
It preserves application credentials. It installs:

```text
/opt/team9/automation/deploy_poll.py
/etc/systemd/system/team9-autodeploy.service
/etc/systemd/system/team9-autodeploy.timer
/etc/systemd/system/docker.service.d/team9-data.conf
```

The Docker drop-in requires `/data` before container restart on boot. Reboot recovery
itself still needs a separate host verification.

Inspect installation and trigger the first check:

```sh
sudo systemctl list-timers --all team9-autodeploy.timer
sudo systemctl start --no-block team9-autodeploy.service
sudo journalctl -u team9-autodeploy.service -n 80 --no-pager
```

The timer can already be running its first check; systemd will not run two copies of
the same service. Follow progress with `sudo journalctl -fu team9-autodeploy.service`.
Leaving the log viewer does not stop deployment. A build can take several minutes.

## How updates work

1. Resolve main over public Git HTTPS. If it matches `DEPLOYMENT_SHA`, do nothing.
2. Query public GitHub CI for that SHA, requiring the latest attempt to be completed
   successfully, on main, from a push to this repository. While waiting, make at most
   one CI API request per two-minute tick. An unchanged deployed main makes no REST call.
3. Download the host deployment script from that exact tested commit. A host lock prevents
   simultaneous deployers. Before building, wait for active work and protected external test
   sessions to finish so Docker builds do not compete with their bounded audits. Builds use
   fresh `/opt/team9/releases/<SHA>` checkouts and the
   existing `measurement` Docker target with `team9-phase3:<SHA>` image tags.
4. Require mounted storage and at least 2 GiB free on the root disk. Build and validate
   before interrupting production. Verify that main has not advanced before activation.
5. Recheck active tester setup/runs, audits, remediations, upload reservations and protected
   external system sessions, waiting up to 2,700 seconds. A PREPARING/READY isolated session
   receives a 2,700-second lease at setup, periodically renewed by authorized business
   activity when less than 2,400 seconds remain, and by successful clock advances, to protect gaps between jobs; older payloads fall back to setup time.
   Expiry only releases this deployment
   guard, without deleting the session or its evidence. Failed/cleaned sessions do not block.
   No public request can set that internal deadline.
   Stop nginx ingress, then gracefully stop all database writers. Recheck the
   stopped databases: if an old request accepted new work while shutdown finished, restart
   the old release and defer without migrating. This closes the initial idle-check race.
6. Back up quiescent system content metadata under the registered
   `/data/deletion-backups/<time>-<SHA>/system.sqlite3` tree, retaining ownership/mode.
   Migrate prior deployment system backups into that registered tree. Keep independent tester
   backups/private configuration in the host-only deployment-backups tree. Preserve the newest
   content-free deletion journal separately; never roll it back with content snapshots. Change only the image/SHA in `compose.env`.
   Start the candidate system, worker and tester with nginx still stopped. Verify their image
   identities, exact ruleset-2 health, trusted isolated-fault configuration, installed Phase 5
   fixture manifest/hashes and 2,400-second suite budget, tester schema and authentication.
   These private probes issue only read requests and create no workbooks or test sessions.
7. Record `/opt/team9/config/publication-state.json` and start nginx only after private checks
   pass. From this point a request may be accepted, so earlier database backups cannot be
   restored automatically. Verify public HTTPS health, images, schema and authentication;
   then record `deployment-state.json`, clear the publication marker and upgrade the poller.

The single-host activation interrupts ingress while writers stop, databases are backed up and
the candidate is checked. Requests racing shutdown may fail. The readiness check does not
replace CI integration or claim a new full live-suite run.

Production Compose explicitly selects ruleset `2.0.0`, the installed Phase 5 manifest and the
required isolated fault and test-clock controls for this designated shared test deployment. These
operational overrides take precedence over retained Phase 3 environment values. Credentials,
reviewer grants, target access maps, bucket/prefix and retained mounts stay in the existing
private configuration. Outside this designated topology, the fault control remains disabled
by default. The image name retains the historical `team9-phase3:<SHA>` prefix; that name does
not select the audit ruleset.

## Failure and recovery

A failed build or preflight leaves the existing release running. Busy-host deferrals retry
on a later timer tick. A failure before nginx publication stops candidate writers, restores the post-drain SQLite
snapshots with their original owner/mode, removes migrated WAL/SHM/journal sidecars, and
restarts the old image and configuration. No public request can have reached the candidate
before that rollback. Immutable source objects are not replaced or rolled back.

A failure once nginx startup was attempted keeps the new backend, configuration and migrated
databases. It does **not** restore old database pages or start an incompatible Phase 3 image:
new user writes may already exist. The durable publication marker remains for diagnosis.
An explicit retry runs only that already-vetted release's local deployment script, verifies
its pinned image/configuration and health, and starts missing services without recreating
running writers. It does not rebuild, migrate or restore data. This recovery runs before
fresh-main deployment gates and still works if main has advanced. After recovery, the next
timer tick applies the ordinary exact-SHA CI gate to any newer main release.

A failed release SHA is recorded in `/opt/team9/config/deploy-failed.json`. A failure or
unfinished-publication marker is checked before the apparent current SHA, so neither the timer
nor a matching `compose.env` can hide an incomplete release. To retry after investigating:

```sh
sudo python3 /opt/team9/automation/deploy_poll.py JGraff24/30861_Team9 --retry
```

For the first upgrade from an older bootstrap, that installed poller may not yet understand
the new publication marker. Its already-vetted local release script is an explicit recovery
entry point; use the full SHA recorded in `publication-state.json`:

```sh
TEAM9_PUBLISHED_SHA=FULL_SHA_FROM_PUBLICATION_STATE
sudo python3 "/opt/team9/releases/$TEAM9_PUBLISHED_SHA/scripts/deploy_host.py" \
  "$TEAM9_PUBLISHED_SHA" JGraff24/30861_Team9
```

This can recover the existing publication even after main advances. Once health passes it
installs that healthy release's updated polling bootstrap. It never substitutes another
candidate image or restores pre-publication data. Both publication/failure marker writes
fsync the file and containing directory before execution proceeds.

Database snapshots are restored only inside the closed-ingress, pre-publication rollback
boundary above. After publication they remain retained recovery evidence and require a
record-preserving recovery procedure. If private rollback fails, services remain stopped and
operators must inspect the journal and backups before reopening ingress.

Deployment status lives in the host journal and deployment-state file, not a GitHub deployment
job. Green GitHub CI alone is not proof that the host updated:

```sh
sudo systemctl show team9-autodeploy.service -p Result -p ExecMainStatus
sudo cat /opt/team9/config/deployment-state.json
curl --connect-timeout 10 --max-time 30 \
  https://3.139.178.70.sslip.io/system/api/v1/health
```

GitHub API/network failures leave production unchanged. This public repository supports
anonymous CI status reads; private repositories would need a different access configuration.
If the API rate limit is reached, later timer ticks retry after GitHub allows requests again.

Images, checkouts and backups are retained. Monitor the 16 GiB root disk and 20 GiB data disk
and remove reviewed obsolete build artifacts as needed. Local backups do not replace off-host
disaster recovery. Deployment preserves EBS/S3 content and registered backups. Phase 5 owner deletion separately purges the selected workbook from retained application-managed copies; preserve its independent deletion journal across restores.
Existing certificate renewal continues using the same Compose project name.

Disable future automatic checks with:

```sh
sudo systemctl disable --now team9-autodeploy.timer
```

An already-running deployment continues. Inspect its service/journal before maintenance.

## References

- [AWS project managed policies](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html)
- [GitHub REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)

## Phase 5 activation prerequisites

Use the [integrated release record](../docs/evidence/phase5_integration.md) and
[backend restore procedure](../docs/implementation/jackson_phase5.md) before activation.
Schema 3 is incompatible with Phase 4 binaries. Inventory/register content backups and verify
versioned S3 deletion permissions and restore fencing. Production declares its own exact target through
`TESTER_OWN_TARGET_URL=https://<SERVER_NAME>/system`; it must already be approved in private
`ALLOWED_TARGET_URLS`. Keep additional cross-team entries in the private `TARGET_TEAM_MAP_JSON`; production selects tester team `team9`, manifest 3.0.0 and
a 2,400-second suite. The source change alone does not update private host configuration.
The 2,700-second guard must defer deployment while evaluations remain protected.
Hosted validation requires deployment of **both the Phase 5 system/website with its matching
worker and the new Phase 5 independent tester**. Successful main CI makes the source eligible
for the host poller; inspect actual deployment state and running identities before testing.
Confirm tester suite 3.0.0 (395 cases) and private own/cross profile configuration. An unchanged
health response or restored Wi-Fi connection does not establish that either release is active.
Record the deployment SHA/image and then retain hosted own-team/reciprocal reports separately
from the local integration evidence.

Production declares its own URL through `TESTER_OWN_TARGET_URL` and the unchanged Team 12 URL
`https://13.184.14.226.sslip.io` as `team12` through `TESTER_DEPLOYMENT_TEAM_MAP_JSON`. Settings
merge these identities with `TARGET_TEAM_MAP_JSON`, preserving additional private entries and
rejecting conflicts. Existing private approvals and target credentials are reused.

The Phase 5 private tester probe validates both declared targets' report profiles and credential
configuration, including the own-team packaged design map, before proxy publication. An invalid
or missing profile/credential entry prevents activation. This read-only configuration check
creates no target/tester sessions and does not establish that Team 12's remote service is ready.
Identity is never inferred from similar hosts or supplied by a caller.
