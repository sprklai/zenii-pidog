#!/usr/bin/env bash
# fix_gpio.sh — Kill all processes holding GPIO and free pins for a clean bridge start

set -uo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}OK${NC}    $*"; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $*"; }
info() { echo -e "  ${YELLOW}----${NC}  $*"; }

echo ""
echo "  GPIO cleanup — freeing pins for PiDog bridge"
echo ""

FREED=false

# 1. Stop systemd services that hold GPIO
for svc in zenii-pidog pidog-bridge pidog; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        info "Stopping ${svc}.service ..."
        sudo systemctl stop "${svc}"
        ok "Stopped ${svc}.service"
        FREED=true
    fi
done

# 2. Kill suspended (Ctrl+Z) and running python bridge/pidog processes
PIDS=$(ps aux | grep -E "python.*bridge|python.*pidog|python.*Pidog" | grep -v grep | awk '{print $2}' || true)
if [[ -n "${PIDS}" ]]; then
    info "Killing stale python processes: ${PIDS}"
    for pid in ${PIDS}; do
        kill -9 "${pid}" 2>/dev/null || sudo kill -9 "${pid}" 2>/dev/null || true
    done
    ok "Killed stale processes"
    FREED=true
else
    ok "No stale processes found"
fi

# 3. Release any zombie jobs in current shell
if jobs -l 2>/dev/null | grep -qE "Stopped|Running"; then
    for pid in $(jobs -p); do
        kill -9 "${pid}" 2>/dev/null || true
    done
    FREED=true
fi

# 4. Only wait if we actually freed something — GPIO kernel release takes ~1s
if [[ "${FREED}" = true ]]; then
    info "Waiting for GPIO to be released by kernel..."
    sleep 1
fi
ok "GPIO cleanup complete"

echo ""
echo "  Start bridge with:"
echo "    PIDOG_CONFIG=/home/neil/pidog-zenii/bridge_config.toml python3 -m bridge"
echo "  Or use: bash restart_bridge.sh"
echo ""
