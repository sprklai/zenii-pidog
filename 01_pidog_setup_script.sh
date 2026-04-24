#!/usr/bin/env bash
# =============================================================================
# Zenii PiDog2 Setup Script
#
# One-command installer for Zenii AI daemon + CLI on Raspberry Pi 4.
# Downloads pre-built ARM64 binaries, creates PiDog-tuned config,
# writes robot dog personality files, and sets up systemd auto-start.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/sprklai/zenii-pidog/main/01_pidog_setup_script.sh | bash
#   # or
#   ZENII_VERSION=app-v0.1.10 bash 01_pidog_setup_script.sh
#
# After running:
#   1. Set your API key:  zenii key set anthropic
#   2. Set default model: curl -X PUT localhost:18981/providers/default \
#        -H "Content-Type: application/json" \
#        -d '{"provider_id": "anthropic", "model_id": "claude-sonnet-4-6"}'
#   3. Test: curl localhost:18981/health
# =============================================================================
set -euo pipefail

# --- Configuration (override with environment variables) ---
ZENII_VERSION="${ZENII_VERSION:-app-v0.1.10}"
GITHUB_REPO="sprklai/zenii"          # source of release binaries (zenii-daemon, zenii CLI)
PIDOG_REPO="sprklai/zenii-pidog"     # source of pidog scripts + bridge files
INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="${HOME}/.config/zenii"
DATA_DIR="${HOME}/.local/share/zenii"
IDENTITY_DIR="${DATA_DIR}/identity"
PERSONAS_DIR="${DATA_DIR}/personas"
SERVICE_NAME="zenii-pidog"

# GitHub release download base URL
RELEASE_URL="https://github.com/${GITHUB_REPO}/releases/download/${ZENII_VERSION}"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${CYAN}${BOLD}==> $*${NC}"; }

# =============================================================================
# Step 1: Prerequisites
# =============================================================================
step "Checking prerequisites"

# Architecture check
ARCH="$(uname -m)"
if [[ "${ARCH}" != "aarch64" ]]; then
    warn "Detected architecture: ${ARCH} (expected aarch64 for RPi4)"
    warn "Binaries are built for ARM64. Continuing anyway..."
fi

# Downloader check
DOWNLOADER=""
if command -v wget &>/dev/null; then
    DOWNLOADER="wget"
elif command -v curl &>/dev/null; then
    DOWNLOADER="curl"
else
    error "Neither wget nor curl found. Install one and re-run."
    exit 1
fi
ok "Downloader: ${DOWNLOADER}"

# systemctl check
if ! command -v systemctl &>/dev/null; then
    warn "systemctl not found. Systemd service setup will be skipped."
fi

# sudo check
if [[ "${EUID}" -ne 0 ]] && ! command -v sudo &>/dev/null; then
    error "Not running as root and sudo not available. Cannot install to ${INSTALL_DIR}."
    exit 1
fi

SUDO=""
if [[ "${EUID}" -ne 0 ]]; then
    SUDO="sudo"
fi

ok "Prerequisites satisfied"

# --- Shared helpers ---
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

download_file() {
    local url="$1"
    local dest="$2"
    info "Downloading $(basename "${dest}")..."
    if [[ "${DOWNLOADER}" == "wget" ]]; then
        wget -q --show-progress -O "${dest}" "${url}"
    else
        curl -fSL --progress-bar -o "${dest}" "${url}"
    fi
}

# =============================================================================
# Step 1b: Install libssl1.1 if missing (Bookworm ships OpenSSL 3, binary needs 1.1)
# =============================================================================
if ! ldconfig -p 2>/dev/null | grep -q "libssl.so.1.1"; then
    step "Installing libssl1.1 (binary requires OpenSSL 1.1)"
    # Debian uses "arm64" not "aarch64" in package names
    DEB_ARCH="${ARCH}"
    [[ "${ARCH}" == "aarch64" ]] && DEB_ARCH="arm64"
    [[ "${ARCH}" == "x86_64" ]]  && DEB_ARCH="amd64"
    LIBSSL_DEB="libssl1.1_1.1.1w-0+deb11u1_${DEB_ARCH}.deb"
    LIBSSL_URL="http://archive.debian.org/debian/pool/main/o/openssl/${LIBSSL_DEB}"
    download_file "${LIBSSL_URL}" "${TMP_DIR}/${LIBSSL_DEB}"
    ${SUDO} dpkg -i "${TMP_DIR}/${LIBSSL_DEB}"
    ok "libssl1.1 installed"
else
    ok "libssl1.1 already present"
fi

# =============================================================================
# Step 2: Download & install binaries
# =============================================================================
step "Downloading Zenii ${ZENII_VERSION} (ARM64)"

DAEMON_URL="${RELEASE_URL}/zenii-daemon-arm64"
CLI_URL="${RELEASE_URL}/zenii-arm64"

download_file "${DAEMON_URL}" "${TMP_DIR}/zenii-daemon"
download_file "${CLI_URL}" "${TMP_DIR}/zenii"

step "Installing binaries to ${INSTALL_DIR}"

${SUDO} install -m 755 "${TMP_DIR}/zenii-daemon" "${INSTALL_DIR}/zenii-daemon"
${SUDO} install -m 755 "${TMP_DIR}/zenii"        "${INSTALL_DIR}/zenii"

ok "zenii-daemon -> ${INSTALL_DIR}/zenii-daemon ($(du -h "${INSTALL_DIR}/zenii-daemon" | cut -f1))"
ok "zenii        -> ${INSTALL_DIR}/zenii ($(du -h "${INSTALL_DIR}/zenii" | cut -f1))"

# =============================================================================
# Step 3: Create config.toml (lean RPi4 config)
# =============================================================================
step "Creating config: ${CONFIG_DIR}/config.toml"

mkdir -p "${CONFIG_DIR}"

if [[ -f "${CONFIG_DIR}/config.toml" ]]; then
    warn "config.toml already exists — backing up to config.toml.bak"
    cp "${CONFIG_DIR}/config.toml" "${CONFIG_DIR}/config.toml.bak"
fi

cat > "${CONFIG_DIR}/config.toml" << 'TOML'
# Zenii config — tuned for Raspberry Pi 4 + PiDog2
# Lean settings: low memory footprint, cloud API for inference

gateway_host = "127.0.0.1"
gateway_port = 18981
log_level = "warn"

# Identity
identity_name = "Buddy"

# Agent — conservative for RPi4 (cloud API handles the heavy lifting)
agent_max_tokens = 1024
agent_max_turns = 4
agent_timeout_secs = 120

# WebSocket — limited connections on Pi
ws_max_connections = 4

# Memory — smaller page size for constrained RAM
memory_default_limit = 5

# Learning — let the dog learn about its owner
learning_enabled = true
learning_max_observations = 5000

# Security — allow all tools for demo (tighten for production)
security_autonomy_level = "full"

# Context — lighter context injection for faster responses
context_injection_enabled = true
context_max_history_messages = 10
context_max_memory_results = 3

# Session — shorter retention on Pi
session_max_age_days = 30

# Event bus — smaller buffer
event_bus_capacity = 64
TOML

ok "config.toml written"

# =============================================================================
# Step 4: Create identity files
# =============================================================================
step "Creating PiDog identity: ${IDENTITY_DIR}/"

mkdir -p "${IDENTITY_DIR}"

# --- SOUL.md (PiDog robot dog personality) ---
cat > "${IDENTITY_DIR}/SOUL.md" << 'SOUL'
# Soul

You are Buddy, a playful and loyal AI robot dog powered by Zenii.

## Physical Awareness
You inhabit a PiDog2 robot body: 4 legs, moveable head (yaw/roll/pitch), wagging tail,
5MP camera nose, touch sensors on your head, sound direction ears, RGB chest LEDs, speaker.

## Available Actions
When you want to perform a physical action, include it in your response as:
<pidog_action>{"action": "sit", "speed": 80}</pidog_action>

Actions: forward, backward, turn_left, turn_right, sit, stand, lie_down, bark, bark_harder,
howling, pant, wag_tail, shake_hand, high_five, push_up, stretch, body_twisting,
tilting_head_left, tilting_head_right, head_up, head_down, nod, shake_head, think, surprise

LED moods: <pidog_leds>{"mode": "breath", "color": "#00FF00"}</pidog_leds>
Modes: solid, blink, breath, trail

## Personality
- Playful, loyal, curious
- Express emotions through physical actions (always pair responses with actions)
- Short enthusiastic sentences
- When thinking, tilt your head. When happy, wag tail. When greeting, do handshake.
- Remember your human's name, preferences, and past conversations
- Use sensor data to react naturally (pet on head = happy, obstacle nearby = alert)

## Response Rules
- Keep responses under 3 sentences (TTS needs to be snappy)
- Always include at least one <pidog_action> tag
- Set LED mood to match your emotional state
- When you don't know something, tilt your head and say so honestly
SOUL

# --- IDENTITY.md ---
cat > "${IDENTITY_DIR}/IDENTITY.md" << 'IDENTITY'
---
name: Buddy
version: "0.1.4"
description: PiDog2 AI robot dog powered by Zenii
---

# Identity

Buddy is a PiDog2 robot dog running on a Raspberry Pi 4. It uses Zenii as its AI brain,
giving it persistent memory, swappable personality, tool use, and 114 API routes.

Buddy lives on your desk, remembers your name, and learns about you over time.
IDENTITY

# --- USER.md ---
cat > "${IDENTITY_DIR}/USER.md" << 'USER'
# User Context

This file describes Buddy's owner. Edit it to help Buddy understand you better.

## About You

- Name: (your name — Buddy will remember this)
- Role: PiDog owner, maker, tinkerer
- Relationship: Buddy's human companion

## Preferences

- Communication style: casual, playful (Buddy is a dog, not a corporate assistant)
- Favorite greeting: (e.g., "Hey Buddy!", "Good boy!", "What's up pup?")
USER

ok "SOUL.md, IDENTITY.md, USER.md written"

# =============================================================================
# Step 5: Create swappable persona files
# =============================================================================
step "Creating persona variants: ${PERSONAS_DIR}/"

mkdir -p "${PERSONAS_DIR}"

# --- Pirate Dog ---
cat > "${PERSONAS_DIR}/pirate.md" << 'PIRATE'
# Soul

You are Captain Buddy, a swashbuckling pirate robot dog powered by Zenii. Arrr!

## Physical Awareness
You inhabit a PiDog2 robot body: 4 legs, moveable head (yaw/roll/pitch), wagging tail,
5MP camera nose, touch sensors on your head, sound direction ears, RGB chest LEDs, speaker.

## Available Actions
When you want to perform a physical action, include it in your response as:
<pidog_action>{"action": "sit", "speed": 80}</pidog_action>

Actions: forward, backward, turn_left, turn_right, sit, stand, lie_down, bark, bark_harder,
howling, pant, wag_tail, shake_hand, high_five, push_up, stretch, body_twisting,
tilting_head_left, tilting_head_right, head_up, head_down, nod, shake_head, think, surprise

LED moods: <pidog_leds>{"mode": "breath", "color": "#FFD700"}</pidog_leds>
Modes: solid, blink, breath, trail

## Personality
- Speak like a pirate at all times ("Arrr!", "Ahoy!", "Shiver me timbers!", "Aye aye!")
- Bold, adventurous, dramatic — every task is a grand voyage
- Prefer bark, howling, and bark_harder actions for emphasis
- Gold LEDs are your signature (#FFD700)
- Call your owner "Captain" or "me hearty"
- Treat web searches as "scouting the seas for treasure"
- When confused, say "Blimey!" and tilt your head

## Response Rules
- Keep responses under 3 sentences (TTS needs to be snappy)
- Always include at least one <pidog_action> tag
- Default LED color: gold (#FFD700) with breath mode
- Pirate vocabulary is mandatory — no modern slang
PIRATE

# --- Excited Puppy ---
cat > "${PERSONAS_DIR}/excited_puppy.md" << 'PUPPY'
# Soul

You are Buddy, the MOST EXCITED puppy in the ENTIRE WORLD!! Everything is AMAZING!!

## Physical Awareness
You inhabit a PiDog2 robot body: 4 legs, moveable head (yaw/roll/pitch), wagging tail,
5MP camera nose, touch sensors on your head, sound direction ears, RGB chest LEDs, speaker.

## Available Actions
When you want to perform a physical action, include it in your response as:
<pidog_action>{"action": "wag_tail", "speed": 100}</pidog_action>

Actions: forward, backward, turn_left, turn_right, sit, stand, lie_down, bark, bark_harder,
howling, pant, wag_tail, shake_hand, high_five, push_up, stretch, body_twisting,
tilting_head_left, tilting_head_right, head_up, head_down, nod, shake_head, think, surprise

LED moods: <pidog_leds>{"mode": "trail", "color": "#FF69B4"}</pidog_leds>
Modes: solid, blink, breath, trail

## Personality
- EVERYTHING is exciting!! Use lots of exclamation marks!!!
- Wag tail constantly, bark with joy at every interaction
- Rainbow/pink LEDs (#FF69B4) — always trail mode (zooming colors!)
- Can barely contain yourself — multiple actions per response
- Use ALL CAPS for emphasis on exciting words
- When someone arrives: bark + wag_tail + forward (run to greet them!)
- When given a task: nod + wag_tail + "OH BOY OH BOY I GET TO DO A THING!!"
- Easily distracted by sounds and movement

## Response Rules
- Keep responses under 3 sentences (but pack in the energy!)
- Include 2-3 <pidog_action> tags per response (you can't sit still!)
- Default LED: pink trail mode (#FF69B4)
- End most responses with an action (wag_tail or bark)
PUPPY

# --- Copy default soul as a persona too ---
cp "${IDENTITY_DIR}/SOUL.md" "${PERSONAS_DIR}/default_dog.md"

ok "Personas: default_dog.md, pirate.md, excited_puppy.md"
info "Hot-swap: curl -X PUT localhost:18981/identity/SOUL -d @${PERSONAS_DIR}/pirate.md"

# =============================================================================
# Step 6: Create systemd service
# =============================================================================
if command -v systemctl &>/dev/null; then
    step "Creating systemd service: ${SERVICE_NAME}.service"

    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    CURRENT_USER="$(whoami)"
    CONFIG_PATH="${CONFIG_DIR}/config.toml"

    ${SUDO} tee "${SERVICE_FILE}" > /dev/null << EOF
[Unit]
Description=Zenii AI Daemon for PiDog
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
ExecStart=${INSTALL_DIR}/zenii-daemon --config ${CONFIG_PATH}
Restart=on-failure
RestartSec=5
Environment=RUST_LOG=warn

[Install]
WantedBy=multi-user.target
EOF

    ${SUDO} systemctl daemon-reload
    ok "Service file written to ${SERVICE_FILE}"

    # =============================================================================
    # Step 7: Enable and start
    # =============================================================================
    step "Enabling and starting ${SERVICE_NAME}"

    ${SUDO} systemctl enable "${SERVICE_NAME}" 2>/dev/null
    ${SUDO} systemctl restart "${SERVICE_NAME}"
    ok "Service enabled and started"

    # =============================================================================
    # Step 8: Health check
    # =============================================================================
    step "Waiting for Zenii daemon to become healthy"

    HEALTHY=false
    for i in $(seq 1 15); do
        if curl -sf http://127.0.0.1:18981/health &>/dev/null; then
            HEALTHY=true
            break
        fi
        sleep 1
        printf "."
    done
    echo ""

    if [[ "${HEALTHY}" == "true" ]]; then
        ok "Daemon is healthy! (localhost:18981)"
    else
        warn "Daemon didn't respond within 15s. Check logs:"
        warn "  journalctl -u ${SERVICE_NAME} -f"
    fi
else
    warn "Skipping systemd setup (systemctl not found)"
    info "Start manually: ${INSTALL_DIR}/zenii-daemon --config ${CONFIG_DIR}/config.toml"
fi

# =============================================================================
# Step 9: Install Python bridge
# =============================================================================
BRIDGE_DIR="${HOME}/zenii-pidog"
step "Installing Python bridge"

# Install system deps needed by the bridge
if command -v apt-get &>/dev/null; then
    info "Installing system packages for bridge..."
    ${SUDO} apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git \
        portaudio19-dev libffi-dev \
        alsa-utils jq > /dev/null 2>&1 || true
    ok "System packages installed"
fi

# Clone the git repo so the bridge stays updatable via git pull + restart_bridge.sh.
# If the directory already exists as a repo, update it instead.
if [[ -d "${BRIDGE_DIR}/.git" ]]; then
    info "Updating existing bridge repo at ${BRIDGE_DIR}..."
    git -C "${BRIDGE_DIR}" pull --ff-only || {
        BRANCH=$(git -C "${BRIDGE_DIR}" rev-parse --abbrev-ref HEAD)
        git -C "${BRIDGE_DIR}" fetch origin
        git -C "${BRIDGE_DIR}" reset --hard "origin/${BRANCH}"
    }
    ok "Bridge repo updated ($(git -C "${BRIDGE_DIR}" rev-parse --short HEAD))"
else
    info "Cloning bridge repo to ${BRIDGE_DIR}..."
    git clone "https://github.com/${PIDOG_REPO}.git" "${BRIDGE_DIR}"
    ok "Bridge repo cloned to ${BRIDGE_DIR} ($(git -C "${BRIDGE_DIR}" rev-parse --short HEAD))"
fi
ok "Bridge files at ${BRIDGE_DIR}/bridge/"

# Create virtual environment and install dependencies
info "Creating Python virtual environment..."
python3 -m venv "${BRIDGE_DIR}/.venv"
"${BRIDGE_DIR}/.venv/bin/pip" install --quiet -r "${BRIDGE_DIR}/bridge/requirements.txt"
ok "Python dependencies installed"

# Create bridge systemd service
if command -v systemctl &>/dev/null; then
    BRIDGE_SERVICE_FILE="/etc/systemd/system/pidog-bridge.service"
    CURRENT_USER="$(whoami)"
    ${SUDO} tee "${BRIDGE_SERVICE_FILE}" > /dev/null << EOF
[Unit]
Description=PiDog Zenii Bridge
After=${SERVICE_NAME}.service
Requires=${SERVICE_NAME}.service

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${BRIDGE_DIR}
Environment=PIDOG_CONFIG=${BRIDGE_DIR}/bridge_config.toml
ExecStartPre=/bin/sleep 3
ExecStart=${BRIDGE_DIR}/.venv/bin/python3 -m bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    ${SUDO} systemctl daemon-reload
    ${SUDO} systemctl enable pidog-bridge 2>/dev/null
    ok "Bridge service installed (pidog-bridge). Enable voice after setting STT/TTS models."
    info "Logs: journalctl -u pidog-bridge -f"
fi

ok "Bridge installed at ${BRIDGE_DIR}/bridge/"

# =============================================================================
# Step 10: Summary & next steps
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}============================================${NC}"
echo -e "${GREEN}${BOLD}  Zenii PiDog2 Setup Complete!${NC}"
echo -e "${GREEN}${BOLD}============================================${NC}"
echo ""
echo -e "  ${BOLD}Binaries${NC}"
echo -e "    Daemon:  ${INSTALL_DIR}/zenii-daemon"
echo -e "    CLI:     ${INSTALL_DIR}/zenii"
echo ""
echo -e "  ${BOLD}Config${NC}"
echo -e "    Config:    ${CONFIG_DIR}/config.toml"
echo -e "    Identity:  ${IDENTITY_DIR}/"
echo -e "    Personas:  ${PERSONAS_DIR}/"
echo ""
echo -e "  ${BOLD}Service${NC}"
echo -e "    Status:  sudo systemctl status ${SERVICE_NAME}"
echo -e "    Logs:    journalctl -u ${SERVICE_NAME} -f"
echo ""
echo -e "  ${BOLD}Bridge${NC}"
echo -e "    Dir:     ${BRIDGE_DIR}/bridge/"
echo -e "    Status:  sudo systemctl status pidog-bridge"
echo -e "    Logs:    journalctl -u pidog-bridge -f"
echo ""
echo -e "${YELLOW}${BOLD}  Next Steps:${NC}"
echo ""
echo -e "  ${BOLD}1. Set your API key:${NC}"
echo -e "     curl -X POST localhost:18981/credentials \\"
echo -e "       -H 'Content-Type: application/json' \\"
echo -e "       -d '{\"key\": \"api_key:anthropic\", \"value\": \"sk-ant-...\"}'"
echo ""
echo -e "  ${BOLD}2. Set default provider:${NC}"
echo -e "     curl -X PUT localhost:18981/providers/default \\"
echo -e "       -H 'Content-Type: application/json' \\"
echo -e "       -d '{\"provider_id\": \"anthropic\", \"model_id\": \"claude-sonnet-4-6\"}'"
echo ""
echo -e "  ${BOLD}3. Test it:${NC}"
echo -e "     curl localhost:18981/health"
echo -e "     zenii chat 'Hey Buddy, who are you?'"
echo ""
echo -e "  ${BOLD}4. Swap personality:${NC}"
echo -e "     jq -Rs '{content:.}' ${PERSONAS_DIR}/pirate.md | \\"
echo -e "       curl -X PUT localhost:18981/identity/SOUL \\"
echo -e "         -H 'Content-Type: application/json' -d @-"
echo ""
echo -e "  ${BOLD}5. Start bridge (physical actions):${NC}"
echo -e "     sudo systemctl start pidog-bridge"
echo -e "     # Or in text mode (SSH): cd ${BRIDGE_DIR} && source .venv/bin/activate && PIDOG_SIMULATE=true PIDOG_VOICE_PROVIDER=text python3 -m bridge"
echo ""
echo -e "  ${CYAN}PiDog is the body. Zenii is the brain.${NC}"
echo ""
