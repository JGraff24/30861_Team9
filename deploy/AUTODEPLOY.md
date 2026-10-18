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
   simultaneous deployers. Builds use fresh `/opt/team9/releases/<SHA>` checkouts and the
   existing `measurement` Docker target with `team9-phase3:<SHA>` image tags.
4. Require mounted storage and at least 2 GiB free on the root disk. Build and validate
   before interrupting production. Verify that main has not advanced before activation.
5. Wait up to 11 minutes for active tester setup/runs and queued/running audits. Take
   online SQLite backups at `/data/deployment-backups/<time>-<SHA>/`.
6. Atomically change only `TEAM9_IMAGE` and `DEPLOYMENT_SHA` in `compose.env`, then recreate
   the system, worker, tester and nginx. Including nginx refreshes its upstream addresses.
7. Require all four containers, the expected application image tags, public HTTPS system
   health, tester schema, and unauthenticated tester rejection. Record the release in
   `/opt/team9/config/deployment-state.json` only after those checks pass. Upgrade the
   polling bootstrap from that successful release as well.

The single-host activation briefly interrupts service. New requests racing that window can
fail. The readiness check does not replace CI integration or claim a new full live-suite run.

## Failure and recovery

A failed build or preflight leaves the existing release running. Busy-host deferrals retry
on a later timer tick. Failed activation restores the previous image and Compose environment,
recreates the containers and checks recovery. A failed release SHA is recorded in
`/opt/team9/config/deploy-failed.json` so the timer does not repeatedly build/restart it.
New main commits are still eligible. To retry the same SHA after investigating:

```sh
sudo python3 /opt/team9/automation/deploy_poll.py JGraff24/30861_Team9 --retry
```

Database backups are retained but **not automatically restored**. An image rollback cannot
guarantee compatibility after future incompatible schema migrations; those need a reviewed
migration/recovery procedure. If rollback also fails, inspect the journal and backups first.

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
disaster recovery. Deployment never deletes EBS, S3 objects, application data or old backups.
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
