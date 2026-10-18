#!/bin/bash
# One-time bootstrap on the existing host; repository access is public HTTPS.
set -euo pipefail
umask 077

release_sha=${1:?Supply the reviewed installer commit SHA}
repository=${2:-JGraff24/30861_Team9}
if [[ ! "$release_sha" =~ ^[0-9a-f]{40}$ ]] ||
   [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo 'Invalid source SHA or repository.' >&2
    exit 1
fi
if [[ $(id -u) != 0 ]]; then
    echo 'Run this installer with sudo on the EC2 host.' >&2
    exit 1
fi
mountpoint -q /data
test -f /opt/team9/config/compose.env
docker compose version >/dev/null

task_dir=$(mktemp -d)
trap 'rm -rf "$task_dir"' EXIT
curl --fail --silent --show-error --location --max-time 60 \
    "https://raw.githubusercontent.com/$repository/$release_sha/scripts/deploy_poll.py" \
    -o "$task_dir/deploy_poll.py"
python3 -m py_compile "$task_dir/deploy_poll.py"
install -d -m 700 /opt/team9/automation
install -m 600 "$task_dir/deploy_poll.py" /opt/team9/automation/deploy_poll.py.next
mv /opt/team9/automation/deploy_poll.py.next /opt/team9/automation/deploy_poll.py

install -d -m 755 /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/team9-data.conf <<'UNIT'
[Unit]
RequiresMountsFor=/data
UNIT

cat > /etc/systemd/system/team9-autodeploy.service <<UNIT
[Unit]
Description=Deploy tested Team 9 main commits
Wants=network-online.target
Requires=docker.service
After=network-online.target docker.service
RequiresMountsFor=/data

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/team9/automation/deploy_poll.py $repository
TimeoutStartSec=45min
TimeoutStopSec=3min
UNIT

cat > /etc/systemd/system/team9-autodeploy.timer <<'UNIT'
[Unit]
Description=Check Team 9 main for tested updates every two minutes

[Timer]
OnBootSec=90s
OnUnitInactiveSec=2min
RandomizedDelaySec=15s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now team9-autodeploy.timer
echo 'Automatic deployment checks enabled. Inspect with:'
echo 'sudo journalctl -u team9-autodeploy.service -n 60 --no-pager'
