#!/usr/bin/env bash
# =============================================================================
# restart_bridge.sh — On-Pi: pull latest code, free GPIO, run bridge
#
# Usage:
#   bash restart_bridge.sh                 # foreground (logs to terminal)
#   bash restart_bridge.sh --service       # restart via systemd instead
#   bash restart_bridge.sh --no-pull       # skip git pull
#
# Environment:
#   PIDOG_CONFIG   Path to bridge_config.toml
#                  (default: /home/<user>/pidog-zenii/bridge_config.toml)
#   SERVICE_NAME   systemd unit name (default: zenii-pidog)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}OK${NC}    $*"; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $*"; }
info() { echo -e "  ${BLUE}INFO${NC}  $*"; }
die()  { echo -e "  ${RED}FAIL${NC}  $*" >&2; exit 1; }

# --- Parse args ---
USE_SERVICE=false
DO_PULL=true
for arg in "$@"; do
    case "$arg" in
        --service)   USE_SERVICE=true ;;
        --no-pull)   DO_PULL=false ;;
        -h|--help)
            echo "Usage: bash restart_bridge.sh [--service] [--no-pull]"
            echo "  --service   restart via systemd instead of running in foreground"
            echo "  --no-pull   skip git pull"
            exit 0 ;;
        *) die "Unknown argument: $arg" ;;
    esac
done

SERVICE_NAME="${SERVICE_NAME:-zenii-pidog}"

# Default config path: same convention as the bridge — sibling dir named pidog-zenii
DEFAULT_CONFIG="/home/${USER}/pidog-zenii/bridge_config.toml"
PIDOG_CONFIG="${PIDOG_CONFIG:-${DEFAULT_CONFIG}}"

VENV_ACTIVATE="/home/${USER}/pidog-zenii/.venv/bin/activate"

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║   Zenii PiDog — Bridge Restart                       ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Mode:    $([ "${USE_SERVICE}" = true ] && echo "systemd (${SERVICE_NAME})" || echo "foreground")"
echo "  Config:  ${PIDOG_CONFIG}"
echo "  Venv:    ${VENV_ACTIVATE}"
echo ""

# =============================================================================
# 1. Git pull (from the script's repo dir)
# =============================================================================
if [[ "${DO_PULL}" = true ]]; then
    if git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree &>/dev/null; then
        info "Pulling latest code in ${SCRIPT_DIR} ..."
        # Stash any local changes so pull doesn't fail
        if ! git -C "${SCRIPT_DIR}" diff --quiet HEAD 2>/dev/null; then
            warn "Local changes detected — stashing before pull"
            git -C "${SCRIPT_DIR}" stash push -m "restart_bridge auto-stash $(date +%Y%m%dT%H%M%S)"
        fi
        git -C "${SCRIPT_DIR}" pull --ff-only || {
            warn "Fast-forward pull failed — fetching and resetting to origin"
            BRANCH=$(git -C "${SCRIPT_DIR}" rev-parse --abbrev-ref HEAD)
            git -C "${SCRIPT_DIR}" fetch origin
            git -C "${SCRIPT_DIR}" reset --hard "origin/${BRANCH}"
        }
        ok "Code up to date ($(git -C "${SCRIPT_DIR}" rev-parse --short HEAD))"
    else
        warn "${SCRIPT_DIR} is not a git repo — skipping pull"
    fi
fi

# =============================================================================
# 2. GPIO cleanup (inline — skips sleep when nothing was running)
# =============================================================================
info "Checking for processes holding GPIO..."

GPIO_FREED=false

# Stop systemd service if active
for svc in "${SERVICE_NAME}" pidog-bridge pidog; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        info "Stopping ${svc}.service ..."
        sudo systemctl stop "${svc}"
        ok "Stopped ${svc}.service"
        GPIO_FREED=true
    fi
done

# Kill stale Python bridge/pidog processes
PIDS=$(ps aux | grep -E "python.*bridge|python.*pidog|python.*Pidog" | grep -v grep | awk '{print $2}' || true)
if [[ -n "${PIDS}" ]]; then
    info "Killing stale python processes: ${PIDS}"
    for pid in ${PIDS}; do
        kill -9 "${pid}" 2>/dev/null || sudo kill -9 "${pid}" 2>/dev/null || true
    done
    ok "Killed stale processes"
    GPIO_FREED=true
else
    ok "No stale processes"
fi

# Only wait if we actually freed something (GPIO kernel release takes ~1s)
if [[ "${GPIO_FREED}" = true ]]; then
    info "Waiting for GPIO to be released by kernel..."
    sleep 1
    ok "GPIO ready"
fi

# =============================================================================
# 3. Verify config and venv exist
# =============================================================================
[[ -f "${PIDOG_CONFIG}" ]] || die "Config not found: ${PIDOG_CONFIG}"
[[ -f "${VENV_ACTIVATE}" ]] || die "Venv not found: ${VENV_ACTIVATE}"
echo ""

# =============================================================================
# 4a. Systemd mode: restart service and tail logs
# =============================================================================
if [[ "${USE_SERVICE}" = true ]]; then
    info "Starting ${SERVICE_NAME} via systemd..."
    sudo systemctl start "${SERVICE_NAME}"
    ok "${SERVICE_NAME} started"
    echo ""
    info "Tailing logs (Ctrl-C to stop watching — service keeps running):"
    echo ""
    journalctl -u "${SERVICE_NAME}" -n 20 -f
    exit 0
fi

# =============================================================================
# 4b. Foreground mode: activate venv and run bridge directly
# =============================================================================
info "Activating venv and starting bridge in foreground..."
echo ""
# shellcheck source=/dev/null
source "${VENV_ACTIVATE}"

exec env PIDOG_CONFIG="${PIDOG_CONFIG}" python3 -m bridge
