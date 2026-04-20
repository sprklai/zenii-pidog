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

# 1. Stop systemd services that hold GPIO
for svc in zenii-pidog pidog-bridge pidog; do
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
        info "Stopping ${svc}.service ..."
        sudo systemctl stop "${svc}"
        ok "Stopped ${svc}.service"
    fi
done

# 2. Kill suspended (Ctrl+Z) and running python bridge/pidog processes
PIDS=$(ps aux | grep -E "python.*bridge|python.*pidog|python.*Pidog" | grep -v grep | awk '{print $2}')
if [[ -n "${PIDS}" ]]; then
    info "Killing stale python processes: ${PIDS}"
    # Try as current user first, then sudo
    for pid in ${PIDS}; do
        kill -9 "${pid}" 2>/dev/null || sudo kill -9 "${pid}" 2>/dev/null || true
    done
    ok "Killed stale processes"
else
    ok "No stale processes found"
fi

# 3. Release any zombie jobs in current shell
while jobs -l | grep -qE "Stopped|Running"; do
    job_pids=$(jobs -p)
    for pid in ${job_pids}; do
        kill -9 "${pid}" 2>/dev/null || true
    done
    break
done

# 4. Wait for GPIO to be released
sleep 2

# 5. Verify GPIO is free
echo ""
info "Verifying GPIO is free ..."
if python3 -c "from pidog import Pidog; d=Pidog(); d.close(); print('GPIO free')" 2>&1 | grep -q "GPIO free"; then
    ok "GPIO is free — ready to start bridge"
else
    warn "GPIO may still be busy — try: sudo reboot"
fi

echo ""
echo "  Start bridge with:"
echo "    PIDOG_CONFIG=/home/neil/pidog-zenii/bridge_config.toml python3 -m bridge"
echo ""
