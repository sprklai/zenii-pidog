#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run() {
    echo "==> Running $1"
    bash "$SCRIPT_DIR/$1"
}

run 01_pidog_setup_script.sh
run 02_post_install_script.sh
run 03_capabilities_test.sh
