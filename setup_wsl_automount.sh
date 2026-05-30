#!/usr/bin/env bash
# setup_wsl_automount.sh — configure E drive to mount automatically at WSL startup
#
# WSL wsl.conf has automount.enabled=false, so Windows drives don't mount
# automatically.  This script creates a systemd service (systemd=true in
# wsl.conf) that mounts E: → /mnt/e before any user services start.
#
# Run once from WSL:
#   bash /mnt/e/Personal_Desktop_Agent/setup_wsl_automount.sh
#
# After this, start_agent_wsl.sh will never need 'sudo mount' again.

set -e

GRN="\033[0;32m"; YLW="\033[0;33m"; RST="\033[0m"
info() { echo -e "${GRN}[automount]${RST} $*"; }
warn() { echo -e "${YLW}[warn]     ${RST} $*"; }

# Detect the current user's uid/gid for the mount options
UID_VAL=$(id -u)
GID_VAL=$(id -g)
USERNAME=$(id -un)

info "Setting up E drive auto-mount for user $USERNAME (uid=$UID_VAL gid=$GID_VAL)"

# ── systemd service ───────────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/mnt-e.mount"

sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Mount Windows E drive (DrvFs)
DefaultDependencies=no
After=-.mount
Before=local-fs.target

[Mount]
What=E:
Where=/mnt/e
Type=drvfs
Options=metadata,uid=${UID_VAL},gid=${GID_VAL},umask=22

[Install]
WantedBy=local-fs.target
EOF

info "Created: $SERVICE_FILE"

# ── Create mount point ────────────────────────────────────────────────────────
sudo mkdir -p /mnt/e
info "Mount point: /mnt/e"

# ── Enable and start ──────────────────────────────────────────────────────────
sudo systemctl daemon-reload
sudo systemctl enable mnt-e.mount
sudo systemctl start  mnt-e.mount

# ── Verify ────────────────────────────────────────────────────────────────────
if mountpoint -q /mnt/e; then
    info "E drive mounted at /mnt/e  ✓"
    ls /mnt/e | head -5
else
    warn "Mount did not complete — check: sudo systemctl status mnt-e.mount"
    exit 1
fi

info ""
info "Done.  E drive will now mount automatically each time WSL starts."
info "The desktop shortcut no longer needs 'sudo mount' in start_agent_wsl.sh."

# ── Update start_agent_wsl.sh to skip the manual mount ───────────────────────
# The script already checks 'mountpoint -q' before attempting mount, so it
# will silently skip the mount step if systemd has already done it.
info "start_agent_wsl.sh is already compatible (mountpoint check at line ~20)."
