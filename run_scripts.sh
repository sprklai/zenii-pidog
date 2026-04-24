#!/usr/bin/env bash
# =============================================================================
# Zenii PiDog2 — Script Runner & Usage Reference
#
# All scripts run on the Pi (RPi4), except 06_sync_bridge.sh which runs on
# the dev machine to push updated bridge code to the Pi.
#
# FIRST-TIME SETUP (run once in order on the Pi):
#   bash run_scripts.sh --setup
#
# BRIDGE UPDATE (run on dev machine after editing bridge/ files):
#   bash run_scripts.sh --sync pi@<PIDOG_IP>
#
# Or run individual scripts as needed — see usage table below.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure all scripts in this directory are executable
chmod +x "${SCRIPT_DIR}"/*.sh 2>/dev/null || true

# --- Colors ---
BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
DIM='\033[2m'
NC='\033[0m'

usage() {
    echo ""
    echo -e "${BOLD}Zenii PiDog2 — Script Reference${NC}"
    echo ""
    echo -e "  ${CYAN}${BOLD}Script${NC}                          ${BOLD}Run on${NC}   ${BOLD}When${NC}"
    echo -e "  ${DIM}──────────────────────────────────────────────────────────────────────${NC}"
    echo -e "  ${CYAN}01_pidog_setup_script.sh${NC}        Pi       First-time install: daemon + bridge"
    echo -e "  ${CYAN}02_post_install_script.sh${NC}       Pi       Verify install (runs after 01)"
    echo -e "  ${CYAN}03_capabilities_test.sh${NC}         Pi       Test Zenii AI + action capabilities"
    echo -e "  ${CYAN}04_bridge_usage_examples.sh${NC}     Pi       Verify bridge connectivity + voice"
    echo -e "  ${CYAN}05_create_bridge_config.sh${NC}      Pi       Generate bridge_config.toml with real keys"
    echo -e "  ${CYAN}06_sync_bridge.sh${NC}               Dev      Push updated bridge/ code → Pi + restart"
    echo ""
    echo -e "  ${BOLD}Modes:${NC}"
    echo ""
    echo -e "  ${GREEN}--setup${NC}           Full first-time setup: 01 → 02 → 05 → 04"
    echo -e "                    Installs daemon + bridge, creates config, verifies voice"
    echo -e "  ${GREEN}--test${NC}            Run 03 to test Zenii AI + action capabilities"
    echo -e "  ${GREEN}--config${NC}          Run 05 to (re)generate bridge_config.toml on this Pi"
    echo -e "  ${GREEN}--verify${NC}          Run 04 to verify voice provider + bridge connectivity"
    echo -e "  ${GREEN}--sync <target>${NC}   Run 06 to sync bridge code from dev machine to Pi"
    echo -e "                    Example: bash run_scripts.sh --sync pi@192.168.1.42"
    echo ""
    echo -e "  ${BOLD}Typical flows:${NC}"
    echo ""
    echo -e "  ${DIM}# Complete first-time setup on a fresh Pi:${NC}"
    echo -e "  bash run_scripts.sh --setup"
    echo ""
    echo -e "  ${DIM}# Re-generate bridge config (new API keys, changed voice provider):${NC}"
    echo -e "  bash run_scripts.sh --config"
    echo ""
    echo -e "  ${DIM}# After editing bridge/ code on dev machine — deploy to Pi:${NC}"
    echo -e "  bash run_scripts.sh --sync pi@<PIDOG_IP>"
    echo ""
    echo -e "  ${DIM}# Verify everything works end-to-end:${NC}"
    echo -e "  bash run_scripts.sh --verify"
    echo ""
    echo -e "  ${DIM}# Run AI + action capability tests:${NC}"
    echo -e "  bash run_scripts.sh --test"
    echo ""
}

run() {
    echo -e "\n${CYAN}==> ${BOLD}$1${NC}"
    bash "${SCRIPT_DIR}/$1"
}

case "${1:-}" in

    --setup)
        echo -e "${BOLD}Running first-time setup (01 → 02 → 05 → 04)...${NC}"
        echo ""
        echo -e "  ${DIM}Step 1/4: Install Zenii daemon + bridge${NC}"
        run 01_pidog_setup_script.sh
        echo ""
        echo -e "  ${DIM}Step 2/4: Verify installation${NC}"
        run 02_post_install_script.sh
        echo ""
        echo -e "  ${DIM}Step 3/4: Generate bridge_config.toml${NC}"
        run 05_create_bridge_config.sh
        echo ""
        echo -e "  ${DIM}Step 4/4: Verify bridge connectivity + voice${NC}"
        run 04_bridge_usage_examples.sh
        echo ""
        echo -e "${GREEN}${BOLD}Setup complete.${NC}"
        echo ""
        echo "  Start the bridge:  bash ~/zenii-pidog/restart_bridge.sh"
        echo "  Test capabilities: bash run_scripts.sh --test"
        ;;

    --test)
        run 03_capabilities_test.sh
        ;;

    --config)
        run 05_create_bridge_config.sh
        ;;

    --verify)
        run 04_bridge_usage_examples.sh
        ;;

    --sync)
        REMOTE="${2:-}"
        if [[ -z "${REMOTE}" ]]; then
            echo -e "${YELLOW}Usage: bash run_scripts.sh --sync user@<PIDOG_IP>${NC}"
            exit 1
        fi
        bash "${SCRIPT_DIR}/06_sync_bridge.sh" "${REMOTE}"
        ;;

    --help|-h|"")
        usage
        ;;

    *)
        echo -e "${YELLOW}Unknown option: $1${NC}"
        usage
        exit 1
        ;;

esac
