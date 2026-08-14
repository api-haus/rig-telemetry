#!/usr/bin/env bash
# Optional boot unit. Why it is optional, and what it adds: docs/runbook.md.
# Usage: sudo tools/install-systemd.sh [--uninstall]

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/rig-telemetry.service
DOCKER="$(command -v docker || echo /usr/bin/docker)"

if [[ $EUID -ne 0 ]]; then
    echo "needs root: sudo $0 $*" >&2
    exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now rig-telemetry.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "removed $UNIT (containers left running; docker compose down to stop them)"
    exit 0
fi

cat > "$UNIT" <<EOF
[Unit]
Description=rig-telemetry monitoring stack
Documentation=file://$REPO/README.md
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$REPO
ExecStart=$DOCKER compose up -d --remove-orphans
ExecStop=$DOCKER compose stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now rig-telemetry.service
systemctl --no-pager status rig-telemetry.service | head -12

echo
echo "installed $UNIT -> $REPO"
echo "verify with: $REPO/tools/rig health"
