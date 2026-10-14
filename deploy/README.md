# Deployment boundary

Bootstrap supplies local factory commands in the top-level README and typed deployment inputs.
Docker images, Compose, reverse proxy, worker CLI, persistent-volume initialization, IAM/S3 policy
and HTTPS runbooks remain Jackson's feature handoff. Nothing here provisions AWS.

Target only: one EC2 host; one system API process and one separate durable worker; EBS-backed
SQLite with WAL/busy timeout; private S3 sources; a separate persistent tester store; stripping
reverse proxy with `/system` and `/tester` ASGI root paths; valid public HTTPS. Confirm topology
with Team 12. Account, region, bucket, budget, DNS, certificate access and a real build SHA must
be supplied before deployment. Do not invent prices or account eligibility; verify current AWS
guidance at deployment. Runtime images should use locked non-dev dependencies, adding the
measurement extra only when enabled.
