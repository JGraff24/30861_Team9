# Automatic deployment to the existing AWS server

The `deploy` workflow deploys a successful **push CI** run for the latest `main` commit.
Pull requests and feature branches cannot deploy. Manual runs of `deploy` also require
successful push CI for the current main SHA. An unset `AWS_DEPLOY_ROLE_ARN` disables
deployment, so merging the workflow alone does not change the running site.

The implementation has local regression checks. AWS OIDC, SSM command execution and a
complete automatic release still need verification against the actual AWS account.

Local verification on October 18, 2026: **552 passed, 6 skipped** in the full regression
suite, including 24 new deployment checks. Ruff lint/format, strict mypy, schema freshness,
actionlint 1.7.12 and cfn-lint all passed. Skips were four opt-in live acceptance checks
and two Linux-only memory enforcement checks on macOS. The host script's Python 3.9 syntax
was checked; a real container deployment and OIDC/SSM execution are not claimed by those tests.

## One-time AWS setup

Use the existing AWS account **629807611072**, region **us-east-2**, and EC2 instance
**i-0922ebb45b0bed415**. Keep the existing `team9-phase3` stack.

1. In IAM, check **Identity providers** for `token.actions.githubusercontent.com`.
   If it exists, copy its ARN. Otherwise leave `ExistingGitHubOidcProviderArn` empty below.
2. Open CloudFormation in us-east-2 and create a **new** stack named `team9-autodeploy`.
   Upload [github-actions.aws.yaml](github-actions.aws.yaml).
3. Keep the supplied instance and GitHub identity defaults. They were checked using
   GitHub's public repository API on October 18, 2026:
   owner `JGraff24` / ID `194822762`, repo `30861_Team9` / ID `1419357952`.
   Supply the existing OIDC provider ARN only if step 1 found one.
4. Acknowledge IAM resource creation and create the stack. Wait for `CREATE_COMPLETE`.
5. Copy the stack Outputs into GitHub repository **Settings → Secrets and variables →
   Actions → Variables** (repository variables, not application tokens):

| CloudFormation output | GitHub variable |
|---|---|
| AWSDeployRoleArn | AWS_DEPLOY_ROLE_ARN |
| AWSDeployDocument | AWS_DEPLOY_DOCUMENT |
| AWSInstanceId | AWS_INSTANCE_ID |
| AWSRegion | AWS_REGION |

The role trusts this repository's immutable owner/repository IDs on `main`. GitHub uses
these immutable subject claims by default for repositories created after July 15, 2026;
this repository was created October 14, 2026. The role can send only this stack's deployment
document to the selected instance and read SSM invocation results. It has no S3 write,
EC2 provisioning or generic `AWS-RunShellScript` permission. No long-lived AWS access key
or SSH key is required. The existing EC2 instance role continues to supply source-bucket access.

The template does not create another EC2 server, bucket, data volume or security group.

## Activate and verify

Merge the changes after CI passes. With the four variables configured, the main push CI
completion starts `deploy` automatically. If setup happens after that CI run, open
**Actions → deploy → Run workflow → main** to deploy the already-tested current commit.
Keep all deployment configuration reviewed on main; main workflows receive the AWS role.

The existing host must have Docker/Compose, Git, curl, Python 3.9+, the `/data` EBS mount,
trusted HTTPS, the current running containers, and these private files from the initial setup:

```text
/opt/team9/config/compose.env
/opt/team9/config/runtime.env
/opt/team9/repo/deploy/compose.production.yaml
```

Do not regenerate application tokens for a release. SSM runs the host script as root.
The first automatic deployment needs no separate host-script installation: the restricted
SSM document downloads it from the exact tested commit before invoking it.

Watch the Actions deployment job and the printed SSM command ID. Success ends with
`DEPLOYED <full SHA>` after verifying the three application images, four running services,
public HTTPS system health, tester OpenAPI, and unauthenticated tester rejection.
Then run the existing live suite when a fresh session is appropriate. The release check is
a readiness check; it does not replace the full CI integration or claim a new 53-case live run.

After a release, reconnect through Session Manager if desired:

```sh
sudo cat /opt/team9/config/deployment-state.json
sudo docker compose --env-file /opt/team9/config/compose.env \
  -f /opt/team9/repo/deploy/compose.production.yaml ps
curl --connect-timeout 10 --max-time 30 \
  https://3.139.178.70.sslip.io/system/api/v1/health
```

## Release and recovery behavior

- GitHub serializes deployment jobs. A host file lock also refuses overlapping deployers,
  including an SSM command left running after a workflow was manually cancelled.
- The host requires `/data` to be mounted and at least 2 GiB free on the root filesystem.
  It verifies main both before checkout/build and before taking the site down. A newer
  main commit supersedes an older successful run, even when the newer commit is not yet green.
- Source checkouts live at `/opt/team9/releases/<SHA>`. Images use `team9-phase3:<SHA>`
  and the existing measurement build target. Build or validation failure leaves the old
  release serving requests.
- It waits up to 11 minutes for active tester setup/runs and queued/running audits, then
  takes online SQLite backups on EBS at `/data/deployment-backups/<time>-<SHA>/`.
  A database error, backup failure or busy timeout aborts before stopping containers.
- Activation briefly interrupts the service. New requests racing the maintenance window
  can fail; this is a single-host deployment, not a zero-downtime rollout.
- It atomically updates only `TEAM9_IMAGE` and `DEPLOYMENT_SHA` in `compose.env`, then
  recreates all services, including nginx so its upstream addresses are refreshed.
  Runtime credentials, source bucket, EBS directories and TLS configuration are retained.
- Failed activation restores the previous Compose environment and image, recreates its
  containers and checks recovery. The workflow still fails so the update is not marked good.
  Successful releases record the active and previous paths/images in `deployment-state.json`.
- Databases are **not automatically restored** on rollback. Future incompatible schema
  migrations require a reviewed migration/recovery procedure; reverting an image cannot
  guarantee compatibility with a migrated database. If rollback also fails, inspect the SSM
  output and retained backups before taking further action.
- Images, checkouts and local database backups are retained. Monitor the 16 GiB root disk
  and 20 GiB data volume and remove reviewed obsolete build artifacts as needed. These local
  backups do not replace off-host disaster recovery. The script never runs `down -v` or
  deletes EBS, S3 objects, application data or older backup directories.
- Existing certificate renewal continues using its current Compose project name. Test
  certificate renewal separately; automatic application deployment does not verify a new
  ACME challenge or host reboot recovery.

If a deployment workflow is cancelled or times out, check its SSM command before retrying.
The AWS command can continue after the GitHub job ends. Host locking prevents simultaneous
activation, and superseded-SHA checks prevent an old queued build from replacing newer main.

## References

- [GitHub OIDC with AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [GitHub OIDC subject formats](https://docs.github.com/en/actions/reference/security/oidc)
- [SSM Run Command status](https://docs.aws.amazon.com/systems-manager/latest/userguide/monitor-commands.html)
- [CloudFormation OIDC provider](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-iam-oidcprovider.html)
