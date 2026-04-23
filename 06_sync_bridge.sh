#!/usr/bin/env bash
# =============================================================================
# Zenii PiDog2 — Sync Bridge Files to Pi
#
# Copies the current go2market/pidog/bridge/ checkout to the Pi's runtime
# directory (/home/<user>/pidog-zenii/bridge) and optionally restarts the
# pidog-bridge systemd service.
#
# Usage (from repo root or go2market/pidog/):
#   bash 06_sync_bridge.sh pi@<PIDOG_IP>
#   BRIDGE_REMOTE_DIR=/home/neil/pidog-zenii bash 06_sync_bridge.sh pi@<PIDOG_IP>
#   RESTART=false bash 06_sync_bridge.sh pi@<PIDOG_IP>               # sync only
#
# Environment:
#   BRIDGE_REMOTE_DIR  Remote runtime dir (default: /home/<remote-user>/pidog-zenii)
#   RESTART            Restart service after sync (default: true)
# =============================================================================
set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}OK${NC}    $*"; }
fail() { echo -e "  ${RED}FAIL${NC}  $*"; exit 1; }
info() { echo -e "  ${BLUE}INFO${NC}  $*"; }

# =============================================================================
# Args
# =============================================================================
REMOTE="${1:-}"
if [[ -z "${REMOTE}" ]]; then
    echo -e "${RED}Usage: bash 06_sync_bridge.sh user@<PIDOG_IP>${NC}"
    echo "  Example: bash 06_sync_bridge.sh pi@192.168.1.42"
    exit 1
fi

RESTART="${RESTART:-true}"

# Derive local bridge source relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BRIDGE="${SCRIPT_DIR}/bridge"

if [[ ! -d "${LOCAL_BRIDGE}" ]]; then
    fail "Bridge source not found at: ${LOCAL_BRIDGE}"
fi

# Derive remote user for default dir
REMOTE_USER="${REMOTE%%@*}"
if [[ "${REMOTE_USER}" == "${REMOTE}" ]]; then
    # No '@' — means just an IP/hostname, use current user
    REMOTE_USER="${USER}"
fi
BRIDGE_REMOTE_DIR="${BRIDGE_REMOTE_DIR:-/home/${REMOTE_USER}/pidog-zenii}"
REMOTE_BRIDGE="${BRIDGE_REMOTE_DIR}/bridge"

# =============================================================================
# Banner
# =============================================================================
echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║   Zenii PiDog2 — Sync Bridge to Pi                   ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Source:  ${LOCAL_BRIDGE}"
echo "  Target:  ${REMOTE}:${REMOTE_BRIDGE}"
echo "  Restart: ${RESTART}"
echo ""

# =============================================================================
# SSH connectivity check
# =============================================================================
info "Testing SSH connectivity to ${REMOTE}..."
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${REMOTE}" true 2>/dev/null; then
    fail "SSH to ${REMOTE} failed. Check host, user, and SSH key."
fi
ok "SSH OK"

# =============================================================================
# Ensure remote bridge dir exists
# =============================================================================
ssh "${REMOTE}" "mkdir -p '${REMOTE_BRIDGE}'"

# =============================================================================
# Compute local content hashes (for drift report)
# =============================================================================
info "Computing local file hashes..."
declare -A LOCAL_SHA
BRIDGE_FILES="__init__.py __main__.py config.py zenii_client.py hardware.py voice.py action_parser.py bridge.py requirements.txt lcd.py"
for f in ${BRIDGE_FILES}; do
    if [[ -f "${LOCAL_BRIDGE}/${f}" ]]; then
        LOCAL_SHA["${f}"]=$(sha256sum "${LOCAL_BRIDGE}/${f}" | cut -c1-8)
    fi
done

# =============================================================================
# Compute remote content hashes
# =============================================================================
info "Computing remote file hashes..."
declare -A REMOTE_SHA
while IFS=$'\t' read -r sha fname; do
    REMOTE_SHA["${fname}"]="${sha}"
done < <(ssh "${REMOTE}" "cd '${REMOTE_BRIDGE}' 2>/dev/null && sha256sum ${BRIDGE_FILES} 2>/dev/null | awk '{print substr(\$1,1,8) \"\t\" \$2}'" || true)

# =============================================================================
# Drift report
# =============================================================================
echo ""
echo -e "  ${BOLD}File drift (local → remote):${NC}"
echo ""
CHANGED=0
for f in ${BRIDGE_FILES}; do
    local_h="${LOCAL_SHA[${f}]:-missing}"
    remote_h="${REMOTE_SHA[${f}]:-missing}"
    if [[ "${local_h}" == "${remote_h}" ]]; then
        echo -e "  ${DIM}  same  ${f} (${local_h})${NC}"
    else
        echo -e "  ${YELLOW}DIFF${NC}  ${f}  ${DIM}remote=${remote_h} → local=${local_h}${NC}"
        CHANGED=$((CHANGED + 1))
    fi
done
echo ""

if [[ "${CHANGED}" -eq 0 ]]; then
    ok "All files match — nothing to sync"
    if [[ "${RESTART}" == "true" ]]; then
        info "No changes, skipping service restart"
    fi
    exit 0
fi

info "${CHANGED} file(s) differ — syncing..."

# =============================================================================
# Rsync bridge files
# =============================================================================
rsync -az --checksum \
    --include="*.py" \
    --include="requirements.txt" \
    --exclude="*" \
    "${LOCAL_BRIDGE}/" \
    "${REMOTE}:${REMOTE_BRIDGE}/"

ok "Bridge files synced to ${REMOTE}:${REMOTE_BRIDGE}"

# Also sync the on-Pi helper scripts (fix_gpio.sh + restart_bridge.sh)
# These live in the repo root and are useful to have up to date on the Pi.
for helper in fix_gpio.sh restart_bridge.sh; do
    if [[ -f "${SCRIPT_DIR}/${helper}" ]]; then
        rsync -az --checksum "${SCRIPT_DIR}/${helper}" "${REMOTE}:/home/${REMOTE_USER}/zenii-pidog/${helper}" 2>/dev/null || true
    fi
done

# =============================================================================
# Optionally restart the systemd service
# =============================================================================
if [[ "${RESTART}" == "true" ]]; then
    echo ""
    info "Restarting pidog-bridge service on ${REMOTE}..."
    if ssh "${REMOTE}" "systemctl is-active pidog-bridge &>/dev/null"; then
        if ssh "${REMOTE}" "sudo systemctl restart pidog-bridge"; then
            ok "pidog-bridge restarted"
            echo ""
            info "Tailing logs (Ctrl-C to stop):"
            echo ""
            ssh -t "${REMOTE}" "journalctl -u pidog-bridge -n 20 -f" || true
        else
            fail "systemctl restart failed — check: ssh ${REMOTE} journalctl -u pidog-bridge -n 50"
        fi
    else
        info "pidog-bridge is not active — skipping restart (run: sudo systemctl start pidog-bridge)"
    fi
fi
