# AWS deployment and local operation

The existing AWS deployment serves the website at
https://3.139.178.70.sslip.io/system/ and the tester API documentation at
https://3.139.178.70.sslip.io/tester/docs. For shared class application credentials and
instructions, use the [Team 9 Class Access Guide (PDF)](../docs/Team9_Class_Access_Guide.pdf).

The hosted tester approves both `https://3.139.178.70.sslip.io/system` and
`https://13.184.14.226.sslip.io`. Their target credentials are configured privately on the
host. Callers authenticate to Team 9's tester with its operator token and select the exact
system base URL in `target_url`; Team 12's target tokens are not tester operator credentials.

For CI-gated updates of the existing AWS host, see [automatic deployment setup](AUTODEPLOY.md).
Automatic deployment preserves private host configuration. Changing target URLs or tokens
requires a private configuration update and tester recreation after active suites finish,
using the existing Compose inputs and persistent tester storage.

The sections below retain the original provisioning and local-operation guidance. The
original preparation validated YAML/JSON locally without running Docker; it predates the
existing operator-led AWS deployment. Provisioning templates are not instructions to
replace or recreate the current AWS resources.

## Files and prerequisites

- `Dockerfile`: pinned uv + locked runtime dependencies, non-root runtime; optional `measurement`
  target installs the locked coverage extra. System, worker and tester use separate processes.
- `compose.yaml`: loopback-only development ports and independent named system/tester volumes.
- `compose.production.yaml`: standalone file; only nginx publishes 443; SQLite has no network port.
- `aws.yaml`: reviewable CloudFormation for one EC2 instance, HTTPS security group, instance role,
  retained encrypted gp3 data volume, private encrypted source bucket and budget notification.
  Existing VPC/subnet and operator-chosen Amazon Linux 2023 AMI are inputs. Nothing auto-deploys.
- `nginx.conf.template`: strips `/system/` and `/tester/`, 26 MiB HTTP body allowance and 1,900-second
  tester timeout. Application file limit remains exactly 25,000,000 bytes (plus 65,536 multipart bytes).
- `iam-role-policy.json.template`, `bucket-policy.json.template`: reviewable policy examples for
  an existing bucket instead of CloudFormation. Do not apply both alternative plans blindly.
- `production.env.example`: required operator inputs; raw credentials belong only in protected
  environment files outside the checkout/image. Never use developer `.env.local` in production.

Before any separately authorized provisioning, Jackson supplies/records:
AWS account, region, existing VPC/subnet/AZ and routing, AMI/architecture, estimated traffic/storage,
instance size, EBS size, exact bucket name, real DNS hostname, certificate/key access and renewal,
private image registry access, deployment SHA/tag, monthly budget/authorized notification address,
operator/SSM access policy, backup retention, and Team 12 URL/credentials/contract confirmation.
A t3.small is only a template starting value; benchmark memory for concurrent upload+audit before
choosing. Budget alerts do not cap spending. Confirm EC2 hours, gp3 capacity, snapshots, S3
capacity/requests, public IPv4, internet egress, registry, TLS/DNS and any NAT/endpoint charges.
Use current regional prices; no numeric monthly price or free-tier eligibility is asserted.

The four application modules are integrated. Repeat the local checkpoint before release:
setup → authenticated clean/seeded upload → separate worker completion → version/audit reads →
real measured coverage → retained tester report. No fakes may be configured in a release image.
Run Linux memory enforcement in that image. The Docker image excludes tests, so the local unit
suite must be run in a Linux development checkout/container with tests mounted and dev dependencies.

## Local Compose

From the repository root, create the private local credentials file, enable testing/measurement as needed,
and configure the tester's exact Docker target/access map privately. Then:

```sh
docker compose -f deploy/compose.yaml config --quiet
docker compose -f deploy/compose.yaml up --build -d
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail 100 system worker
docker compose -f deploy/compose.yaml restart system worker
docker compose -f deploy/compose.yaml down
```

No `down -v`: that removes durable volumes. Concrete factories are selected by default;
health returns 200 when storage and required integrations are available. Use
`TEAM9_BUILD_TARGET=measurement` for development Compose when enabling code measurement.
The tester is implemented and its manifest/fixtures are included in the installed wheel.
The Docker build also copies the shared wire-schema source before building that wheel.
Production selects its bundled Phase 4 manifest at
`/service/.venv/lib/python3.12/site-packages/tester/fixtures/manifest-phase4.json`; there is no
source checkout under `/service/tester` in the runtime image.
The Docker build copies fixture inputs before packaging and runs the installed wheel so
package-relative fixture/UI paths resolve. Validate local image/Compose changes separately
before applying them to the existing deployment.

## Render and review before provisioning

`AWS_ACCOUNT_ID`, `AWS_REGION`, `S3_BUCKET`, `SERVER_NAME`, `MONTHLY_BUDGET_USD` must already
contain the real operator-provided values. Rendering is local and refuses existing files:

```sh
uv run --locked python -m scripts.prepare_deploy --output /tmp/team9-deploy-review
```

Review the rendered policies and proxy. Template rendering validates shapes/JSON, not AWS
permissions, hostname ownership or TLS. Review `aws.yaml` with CloudFormation validation and
IAM Access Analyzer using an authorized account before creating a change set. Validate the
intended AMI, matching subnet/VPC/AZ, HTTPS egress to registry/pod and SSM connectivity. Choose
a subnet with the needed public route/address or separately reviewed ingress design. The
security group opens only 443; no 22/8000/8001/database ingress. Administrative IAM principals
must be explicitly allowed SSM sessions; the instance role only supplies SSM core and the source
prefix operations. Do not attach administrator permissions to the application role.

After separate user authorization, an operator can create/review a CloudFormation change set
with `CAPABILITY_IAM`, inspect billable resources, then explicitly execute it. This handoff has
not performed `aws cloudformation deploy`, `create-stack`, `execute-change-set`, or any equivalent.
The `AWS_ACCOUNT_ID` is checked locally as an input, but the AWS caller identity must be verified
separately in the account used for the future change set.

## Host, persistent storage and private S3

On the authorized host, install Docker Engine + Compose using the selected OS's official
instructions. Establish SSM administrative access. Check `lsblk -f` and the volume ID against
CloudFormation outputs before mounting; Nitro device names may differ. Format only a verified
new empty data volume. Never format a retained/restored volume. Mount by UUID at the chosen
`EBS_DATA_DIR`, persist it in `/etc/fstab`, and require successful mount before starting Compose.
A host boot without EBS must fail service startup rather than silently write to the root disk.
Create `system` and `tester` subdirectories owned by UID/GID 10001 with private permissions.
SQLite must be on local EBS storage, not NFS/EFS or a multi-host share.

For a preexisting bucket, verify all four S3 Block Public Access controls, bucket-owner-enforced
ownership, default SSE-S3 AES256 encryption and region; apply reviewed TLS/encryption/create-only
policies. Use the EC2 instance profile credential chain; the adapter rejects static environment/shared-file credentials and accepts only SDK iam-role/container-role providers. IMDSv2 is
required and hop limit 2 supports the API/worker containers. Constrain tester access to metadata
at host/container networking level before release; setting `AWS_EC2_METADATA_DISABLED` alone
only disables the SDK path. The API/worker child guard receives no AWS or bearer environment.

`IfNoneMatch='*'` prevents overwrite, SHA256 is submitted to S3 and verified on read, and no
public ACL is sent. The role can Get/Put/Delete only the chosen source prefix; ListBucket is
used by HeadBucket health. DeleteObject is required for compensating failed-upload cleanup
and explicit test cleanup. IAM cannot distinguish a committed key from an uncommitted key in
this application design; internal code is the deletion boundary. There is no ordinary deletion
HTTP endpoint. Bucket versioning/Object Lock are not enabled in this template: adding them
requires lifecycle/cleanup/retention design and cost review.

## Build, TLS, release and startup

Record a reviewed source SHA and immutable release tag → SHA → image digest mapping in
`docs/evidence/jackson_phase3.md` when a real release is made. The integrated source is tracked in `docs/evidence/phase3_integration.md`; use the reviewed
merged commit for a release. No release tag/digest is invented. Build for the
selected EC2 architecture using the locked dependencies, scan the image, and place it in the
approved private registry. Supply `TEAM9_IMAGE` as an immutable tag/digest, never mutable latest.

Copy the reviewed nginx rendering to `deploy/nginx.conf` on the host (do not commit private
configuration). Put a trusted hostname-matching full chain and private key in `TLS_CERT_DIR`
as `fullchain.pem`/`privkey.pem` with restricted host permissions. Establish tested renewal and
nginx reload. No self-signed/expired/wrong-host certificate is a production substitute. Configure
DNS to the authorized public endpoint. Fill `production.env.example` privately for Compose
interpolation, and `TEAM9_ENV_FILE` with production hashes/grants/factories/tester access. Do not
put any credential values into a shell command or source untrusted environment files.

ASGI settings must use `API_ROOT_PATH=/system` and `TESTER_ROOT_PATH=/tester`. A private file
may hold tester target tokens and operator hashes; keep it outside release directories, readable
only by the operator. The production Compose loader passes only configured values, no secrets
are copied into images. Application and proxy access logs are disabled to avoid request metadata
leakage; application allowlisted JSON events remain enabled.

After loading the private *non-secret Compose interpolation* variables into the operator's
approved environment, from the release checkout:

```sh
docker compose -f deploy/compose.production.yaml config --quiet
docker compose -f deploy/compose.production.yaml pull
docker compose -f deploy/compose.production.yaml up -d
docker compose -f deploy/compose.production.yaml ps
docker compose -f deploy/compose.production.yaml logs --tail 100 system worker
```

Do not run `compose config` without `--quiet` in recorded logs: env_file values can be expanded.
Install a host boot service that depends on the EBS mount and Docker, loads the protected
Compose interpolation file, runs the exact `up -d` command, and uses the same immutable release
path. `restart: unless-stopped` restarts containers; the mount dependency protects host reboots.
One API and one worker only. Nginx readiness can race startup briefly; check it after all processes
are up. Verify a valid certificate using an HTTPS client without `-k`, and test `/system/docs`,
`/tester/docs`, `/system/api/v1/health`, rejected credentials and authenticated real workbook
flow. Check security groups and host sockets to prove direct APIs are unreachable publicly.
The grader uses `https://$SERVER_NAME/system` and `https://$SERVER_NAME/tester` bases, never `/test`
suffixes/credentials/queries. Grader tester-run read timeout must be at least 1,830 seconds for the Phase 4 suite.

## Recovery, backup, cleanup and rollback

On API restart and authorized status polling, expired queue deadlines/leases become controlled
failures. Workers also recover them before a claim. A live lease is never stolen; no audit is
replayed after lease expiration. The worker polls every 0.25s and queue deadline defaults to 15s.
Under sustained load, uploads may accept then fail with `QUEUE_TIMEOUT`; tune with measured
load and agreed tester polling budgets. An unavailable DB may prevent immediate failure storage;
recovery records the failure when storage returns. JSON logs emit IDs, status/code/duration,
not workbook data/tokens/alt text. No Prometheus/CloudWatch metrics pipeline is installed.

Use SQLite's online backup API (or stop both writers before a full volume snapshot); copying
only the live `.sqlite3` file can lose WAL data. An operator backup example inside the running
system container is:

```sh
docker compose -f deploy/compose.production.yaml exec -T system python -c 'import sqlite3; src=sqlite3.connect("/service/state/system.sqlite3"); dst=sqlite3.connect("/service/state/backup.sqlite3"); src.backup(dst); dst.close(); src.close()'
```

Copy the backup to the separately authorized encrypted backup destination; snapshot EBS with a
retention plan and back up tester data separately using Dixon's final storage procedure. Source
bytes in S3 must be retained for referenced keys. Test restoring metadata+source keys in an
isolated environment before calling backups verified. The example backup file is inside EBS
and is not itself off-host protection. Snapshot/backup copy actions incur costs and remain
unexecuted. The application schema currently supports version 1 only; startup refuses newer
schemas. Future migrations require a matching restore/rollback plan.

For scoped test resets, Ryan's future SessionService must call `begin_session_cleanup` with
the owning administrator; it rejects active jobs and reserves CLEANED, returns only that test
namespace's blob keys, deletes with `delete_test_source`, then calls `finish_session_cleanup`.
Record partial deletion with `mark_session_failed`; retry the same reservation. Never reopen
CLEANED or wipe global tables/blobs. These repository primitives are tested; no standalone reset
HTTP operation or CLI bypass is delivered. Ordinary workbook deletion remains future work.

For rollback, stop API+worker, retain EBS/S3, select the previous reviewed image digest and its
source SHA, ensure schema compatibility, then restart and run readiness/integrity checks. If a
schema migration is incompatible, restore the matching backup in an isolated volume first.
Do not rewrite completed audit history or requeue stale work. Keep failed sources inspectable.
If blob rollback fails or a process dies between blob put and metadata commit, an unreferenced
blob can remain. Reconcile keys against metadata/backups during an operator maintenance window;
never automatically delete committed/unknown keys. Full reconciliation tooling is Phase 4 work.

## References checked during preparation (2026-10-16)

- [S3 conditional write enforcement](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html): basis for create-only policy; this implementation uses single PutObject, not multipart S3 upload.
- [S3 PutObject API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html): conditional headers, encryption and checksums used by the adapter.
- [EC2 security groups](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-security-groups.html): operator ingress review reference.
- [Using EBS volumes](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-using-volumes.html): identify devices and avoid formatting retained data.
- [Instance metadata options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html): operator review for containers and role access.
- [EC2 pricing](https://aws.amazon.com/ec2/pricing/): verify current region/account costs at deployment; no price quote is recorded here.

## Phase 4 activation and schema recovery

Use the automatic release process described in [AUTODEPLOY.md](AUTODEPLOY.md) for the existing
host. Production Compose explicitly overrides retained Phase 3 ruleset/manifest values and
enables the required test-only audit-failure control on this designated shared test target;
it leaves private credentials, grants, access maps and storage locations intact. Ordinary
data cannot consume that control.

Schema 2 cannot be opened by the old Phase 3 repository. Activation therefore closes ingress,
stops all old writers, refuses raced active work, and snapshots the resulting stores before
starting the candidate privately. Only a failure before proxy publication may restore those
snapshots and the old image. After proxy startup is attempted, automatic recovery retains the
new databases and image, records the unfinished publication and requires an explicit safe
retry. Never restore a pre-release backup over data accepted after publication.
