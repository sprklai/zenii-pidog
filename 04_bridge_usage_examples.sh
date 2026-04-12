#!/usr/bin/env bash
# =============================================================================
# Zenii PiDog2 Bridge Usage Examples
#
# Interactive setup + verification script for the Python bridge that connects
# PiDog2 hardware to the Zenii AI daemon.
#
# Guides you through picking a voice provider, storing API keys in Zenii,
# verifying all prerequisites, and testing bridge connectivity -- so you
# know everything is ready before hitting record (or deploying to production).
#
# Usage:
#   bash 04_bridge_usage_examples.sh
#
# Prerequisites:
#   - 01_pidog_setup_script.sh has been run (daemon + bridge installed)
#   - Daemon is running: sudo systemctl status zenii-pidog
# =============================================================================
set -uo pipefail

# --- Config (override via environment) ---
ZENII_URL="${ZENII_URL:-http://127.0.0.1:18981}"
BRIDGE_DIR="${BRIDGE_DIR:-${HOME}/pidog-zenii}"
CURL_TIMEOUT=10
PASS=0
FAIL=0
SKIP=0

# Selected provider (set during menu)
VOICE_PROVIDER=""

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

pass()    { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC}  $*"; }
fail()    { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC}  $*"; }
skip()    { SKIP=$((SKIP + 1)); echo -e "  ${YELLOW}SKIP${NC}  $*"; }
divider() { echo -e "\n${DIM}────────────────────────────────────────────────────────${NC}"; }
header()  { echo -e "\n${CYAN}${BOLD}[$1]${NC} ${BOLD}$2${NC}"; }
info()    { echo -e "  ${BLUE}[INFO]${NC} $*"; }
warn()    { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
cmd()     { echo -e "  ${DIM}\$${NC} $*"; }

api() { curl -sf --max-time "${CURL_TIMEOUT}" "$@" 2>&1; }

# =============================================================================
# Banner
# =============================================================================
echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║   Zenii PiDog2 — Bridge Setup & Usage Examples       ║"
echo "  ║   Voice provider selection + connectivity tests       ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

# =============================================================================
# [0] Preflight
# =============================================================================
header "0" "Preflight Checks"

# jq
if command -v jq &>/dev/null; then
    pass "jq found: $(jq --version)"
else
    fail "jq not found — install: sudo apt-get install -y jq"
    exit 1
fi

# curl
if command -v curl &>/dev/null; then
    pass "curl found"
else
    fail "curl not found — install: sudo apt-get install -y curl"
    exit 1
fi

# python3 >= 3.9
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(echo "${PY_VER}" | cut -d. -f1)
    PY_MINOR=$(echo "${PY_VER}" | cut -d. -f2)
    if [[ "${PY_MAJOR}" -ge 3 && "${PY_MINOR}" -ge 9 ]]; then
        pass "python3 ${PY_VER} (>= 3.9)"
    else
        fail "python3 ${PY_VER} is too old — need 3.9+"
    fi
else
    fail "python3 not found — install: sudo apt-get install -y python3"
    exit 1
fi

# Bridge directory
if [[ -d "${BRIDGE_DIR}" ]]; then
    pass "Bridge dir: ${BRIDGE_DIR}"
else
    fail "Bridge dir not found: ${BRIDGE_DIR}"
    info "Run 01_pidog_setup_script.sh first, or set BRIDGE_DIR env var."
    exit 1
fi

# Python venv — create and install deps automatically if missing
if [[ -f "${BRIDGE_DIR}/.venv/bin/python3" ]]; then
    pass "Python venv: ${BRIDGE_DIR}/.venv"
else
    info "Python venv not found — creating it now..."
    if python3 -m venv "${BRIDGE_DIR}/.venv" 2>&1 | sed 's/^/    /'; then
        pass "Python venv created: ${BRIDGE_DIR}/.venv"
    else
        fail "Failed to create Python venv at ${BRIDGE_DIR}/.venv"
        exit 1
    fi
fi

# Core requirements — install/upgrade if needed
if "${BRIDGE_DIR}/.venv/bin/python3" -c "import aiohttp" &>/dev/null 2>&1; then
    pass "Core requirements installed (aiohttp present)"
else
    info "Installing core requirements from bridge/requirements.txt..."
    if "${BRIDGE_DIR}/.venv/bin/pip" install -q -r "${BRIDGE_DIR}/bridge/requirements.txt" 2>&1 | tail -5 | sed 's/^/    /'; then
        pass "Core requirements installed"
    else
        fail "pip install failed — check network or bridge/requirements.txt"
        exit 1
    fi
fi

# Bridge package
if [[ -f "${BRIDGE_DIR}/bridge/bridge.py" ]]; then
    pass "Bridge package: ${BRIDGE_DIR}/bridge/"
else
    fail "Bridge files missing from ${BRIDGE_DIR}/bridge/"
    info "Run 01_pidog_setup_script.sh to install the bridge."
    exit 1
fi

# Daemon health
if api "${ZENII_URL}/health" &>/dev/null; then
    pass "Daemon healthy at ${ZENII_URL}"
else
    fail "Daemon not responding at ${ZENII_URL}"
    info "Start it: sudo systemctl start zenii-pidog"
    info "Or manually: zenii-daemon --config ~/.config/zenii/config.toml"
    exit 1
fi

echo ""

# =============================================================================
# [1] Voice Provider Selection
# =============================================================================
header "1" "Voice Provider Selection"

echo ""
echo -e "  ${BOLD}Choose a voice provider for the bridge:${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} ${BOLD}text${NC}        — stdin/stdout (SSH-friendly, no audio hardware)"
echo -e "     ${DIM}Best for: development, demos over SSH, CI testing${NC}"
echo ""
echo -e "  ${CYAN}2)${NC} ${BOLD}local${NC}       — Vosk STT + Piper TTS (fully offline, RPi4)"
echo -e "     ${DIM}Best for: demos without internet, privacy-sensitive deployments${NC}"
echo -e "     ${DIM}Latency: ~1s STT + ~1s TTS | Cost: free${NC}"
echo ""
echo -e "  ${CYAN}3)${NC} ${BOLD}pipecat-dc${NC}  — Deepgram STT + Cartesia TTS (cloud, low-latency)"
echo -e "     ${DIM}Best for: video demos, best voice quality, viral video recording${NC}"
echo -e "     ${DIM}Latency: ~300ms STT + ~200ms TTS | Cost: pay-per-use${NC}"
echo ""
echo -e "  ${CYAN}4)${NC} ${BOLD}pipecat-de${NC}  — Deepgram STT + ElevenLabs TTS (cloud, voice cloning)"
echo -e "     ${DIM}Best for: custom dog voices, character voice cloning${NC}"
echo -e "     ${DIM}Latency: ~300ms STT + ~500ms TTS | Cost: pay-per-use${NC}"
echo ""

read -rp "  Select provider [1-4]: " CHOICE

case "${CHOICE}" in
    1) VOICE_PROVIDER="text"       ;;
    2) VOICE_PROVIDER="local"      ;;
    3) VOICE_PROVIDER="pipecat-dc" ;;
    4) VOICE_PROVIDER="pipecat-de" ;;
    *)
        warn "Invalid choice '${CHOICE}'. Defaulting to text mode."
        VOICE_PROVIDER="text"
        ;;
esac

echo ""
pass "Voice provider selected: ${VOICE_PROVIDER}"

# =============================================================================
# [2] API Key Setup (pipecat providers only)
# =============================================================================
if [[ "${VOICE_PROVIDER}" == pipecat-* ]]; then
    header "2" "API Key Setup — ${VOICE_PROVIDER}"
    echo ""

    # --- Deepgram (STT for both pipecat options) ---
    echo -e "  ${BOLD}Deepgram${NC} (Speech-to-Text)"
    if [[ -n "${PIPECAT_STT_API_KEY:-}" ]]; then
        skip "PIPECAT_STT_API_KEY already set in environment"
    else
        echo -n "  Enter Deepgram API key (from console.deepgram.com): "
        read -rs DG_KEY
        echo ""
        if [[ -n "${DG_KEY}" ]]; then
            STORE_RESP=$(api -X POST "${ZENII_URL}/credentials" \
                -H "Content-Type: application/json" \
                -d "{\"key\": \"api_key:deepgram\", \"value\": \"${DG_KEY}\"}" 2>&1) && \
                pass "Deepgram key stored (api_key:deepgram)" || \
                fail "Failed to store Deepgram key: ${STORE_RESP}"
            export PIPECAT_STT_API_KEY="${DG_KEY}"
        else
            skip "No Deepgram key entered — skipping"
        fi
    fi

    echo ""

    # --- TTS provider (Cartesia or ElevenLabs) ---
    if [[ "${VOICE_PROVIDER}" == "pipecat-dc" ]]; then
        echo -e "  ${BOLD}Cartesia${NC} (Text-to-Speech)"
        if [[ -n "${PIPECAT_TTS_API_KEY:-}" ]]; then
            skip "PIPECAT_TTS_API_KEY already set in environment (Cartesia)"
        else
            echo -n "  Enter Cartesia API key (from play.cartesia.ai): "
            read -rs CA_KEY
            echo ""
            if [[ -n "${CA_KEY}" ]]; then
                STORE_RESP=$(api -X POST "${ZENII_URL}/credentials" \
                    -H "Content-Type: application/json" \
                    -d "{\"key\": \"api_key:cartesia\", \"value\": \"${CA_KEY}\"}" 2>&1) && \
                    pass "Cartesia key stored (api_key:cartesia)" || \
                    fail "Failed to store Cartesia key: ${STORE_RESP}"
                export PIPECAT_TTS_API_KEY="${CA_KEY}"
            else
                skip "No Cartesia key entered — skipping"
            fi
        fi
        echo ""
        echo -n "  Enter Cartesia Voice ID (leave blank for default): "
        read -r CA_VOICE
        if [[ -n "${CA_VOICE}" ]]; then
            export PIPECAT_TTS_VOICE="${CA_VOICE}"
            info "Voice ID set: ${CA_VOICE}"
        fi
    else
        echo -e "  ${BOLD}ElevenLabs${NC} (Text-to-Speech)"
        if [[ -n "${PIPECAT_TTS_API_KEY:-}" ]]; then
            skip "PIPECAT_TTS_API_KEY already set in environment (ElevenLabs)"
        else
            echo -n "  Enter ElevenLabs API key (from elevenlabs.io): "
            read -rs EL_KEY
            echo ""
            if [[ -n "${EL_KEY}" ]]; then
                STORE_RESP=$(api -X POST "${ZENII_URL}/credentials" \
                    -H "Content-Type: application/json" \
                    -d "{\"key\": \"api_key:elevenlabs\", \"value\": \"${EL_KEY}\"}" 2>&1) && \
                    pass "ElevenLabs key stored (api_key:elevenlabs)" || \
                    fail "Failed to store ElevenLabs key: ${STORE_RESP}"
                export PIPECAT_TTS_API_KEY="${EL_KEY}"
            else
                skip "No ElevenLabs key entered — skipping"
            fi
        fi
        echo ""
        echo -n "  Enter ElevenLabs Voice ID (leave blank for default): "
        read -r EL_VOICE
        if [[ -n "${EL_VOICE}" ]]; then
            export PIPECAT_TTS_VOICE="${EL_VOICE}"
            info "Voice ID set: ${EL_VOICE}"
        fi
    fi
else
    header "2" "API Key Setup"
    skip "No API keys needed for '${VOICE_PROVIDER}' mode"
fi

# =============================================================================
# [3] Prerequisite Tests (per provider)
# =============================================================================
header "3" "Prerequisite Tests — ${VOICE_PROVIDER}"

case "${VOICE_PROVIDER}" in

    "text")
        pass "text mode: no audio hardware or models required"
        ;;

    "local")
        # Vosk model
        VOSK_MODEL="${PIDOG_STT_MODEL:-${BRIDGE_DIR}/models/vosk-model-small-en-us-0.15}"
        if [[ -d "${VOSK_MODEL}" ]]; then
            pass "Vosk model found: ${VOSK_MODEL}"
        else
            fail "Vosk model not found: ${VOSK_MODEL}"
            info "Download it:"
            cmd "mkdir -p ${BRIDGE_DIR}/models"
            cmd "wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
            cmd "unzip vosk-model-small-en-us-0.15.zip -d ${BRIDGE_DIR}/models/"
            cmd "export PIDOG_STT_MODEL=${BRIDGE_DIR}/models/vosk-model-small-en-us-0.15"
        fi

        # Piper binary
        if command -v piper &>/dev/null; then
            pass "Piper binary found: $(which piper)"
        else
            fail "Piper TTS binary not found in PATH"
            info "Install piper:"
            cmd "wget https://github.com/rhasspy/piper/releases/latest/download/piper_linux_aarch64.tar.gz"
            cmd "tar -xzf piper_linux_aarch64.tar.gz"
            cmd "sudo mv piper/piper /usr/local/bin/"
        fi

        # Piper voice model
        TTS_MODEL="${PIDOG_TTS_MODEL:-${BRIDGE_DIR}/models/piper/en_US-ryan-low.onnx}"
        if [[ -f "${TTS_MODEL}" ]]; then
            pass "Piper voice model found: ${TTS_MODEL}"
        else
            fail "Piper voice model not found: ${TTS_MODEL}"
            info "Download the model:"
            cmd "mkdir -p ${BRIDGE_DIR}/models/piper"
            cmd "wget -P ${BRIDGE_DIR}/models/piper/ \\"
            cmd "    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/low/en_US-ryan-low.onnx"
            cmd "wget -P ${BRIDGE_DIR}/models/piper/ \\"
            cmd "    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/low/en_US-ryan-low.onnx.json"
        fi

        # ALSA / audio
        if command -v arecord &>/dev/null; then
            pass "ALSA tools found (arecord)"
        else
            warn "arecord not found — install: sudo apt-get install -y alsa-utils"
        fi
        ;;

    "pipecat-dc"|"pipecat-de")
        # aiohttp (required for direct REST calls to cloud providers)
        if "${BRIDGE_DIR}/.venv/bin/python3" -c "import aiohttp" &>/dev/null 2>&1; then
            pass "aiohttp installed in venv (cloud REST provider)"
        else
            info "aiohttp missing — installing requirements..."
            if "${BRIDGE_DIR}/.venv/bin/pip" install -q -r "${BRIDGE_DIR}/bridge/requirements.txt"; then
                pass "aiohttp installed"
            else
                fail "pip install failed"
            fi
        fi

        # Verify Deepgram key in credential store
        CREDS_RESP=$(api "${ZENII_URL}/credentials" 2>&1)
        if echo "${CREDS_RESP}" | grep -q "api_key:deepgram"; then
            pass "Deepgram key in credential store"
        elif [[ -n "${PIPECAT_STT_API_KEY:-}" ]]; then
            pass "Deepgram key in environment (PIPECAT_STT_API_KEY)"
        else
            fail "Deepgram key not found — enter it in step [2] or set PIPECAT_STT_API_KEY"
        fi

        # Verify TTS key
        if [[ "${VOICE_PROVIDER}" == "pipecat-dc" ]]; then
            if echo "${CREDS_RESP}" | grep -q "api_key:cartesia"; then
                pass "Cartesia key in credential store"
            elif [[ -n "${PIPECAT_TTS_API_KEY:-}" ]]; then
                pass "Cartesia key in environment (PIPECAT_TTS_API_KEY)"
            else
                fail "Cartesia key not found — enter it in step [2] or set PIPECAT_TTS_API_KEY"
            fi
        else
            if echo "${CREDS_RESP}" | grep -q "api_key:elevenlabs"; then
                pass "ElevenLabs key in credential store"
            elif [[ -n "${PIPECAT_TTS_API_KEY:-}" ]]; then
                pass "ElevenLabs key in environment (PIPECAT_TTS_API_KEY)"
            else
                fail "ElevenLabs key not found — enter it in step [2] or set PIPECAT_TTS_API_KEY"
            fi
        fi
        ;;
esac

# =============================================================================
# [4] Bridge Connectivity Test
# =============================================================================
header "4" "Bridge Connectivity Test"
echo ""
info "Starting bridge for 5s to verify daemon connection (PIDOG_SIMULATE=true)..."
echo ""

BRIDGE_LOG=$(cd "${BRIDGE_DIR}" && \
    PIDOG_SIMULATE=true PIDOG_VOICE_PROVIDER=text \
    timeout --kill-after=2 5 .venv/bin/python3 -m bridge </dev/null 2>&1 || true)

echo "${BRIDGE_LOG}" | head -15 | sed 's/^/    /'
echo ""

if echo "${BRIDGE_LOG}" | grep -qi "connected\|healthy\|waiting\|ready\|listening"; then
    pass "Bridge connected to daemon successfully"
elif echo "${BRIDGE_LOG}" | grep -qi "error\|refused\|failed\|cannot"; then
    fail "Bridge connection error — check logs above"
    info "Common fixes:"
    info "  - Daemon not running: sudo systemctl start zenii-pidog"
    info "  - Token mismatch: check ZENII_TOKEN matches daemon config"
else
    pass "Bridge started without fatal errors"
fi

# =============================================================================
# [5] Run Command Reference
# =============================================================================
header "5" "Run Command — ${VOICE_PROVIDER}"
echo ""
echo -e "  ${BOLD}Use this command to start the bridge:${NC}"
echo ""

case "${VOICE_PROVIDER}" in

    "text")
        echo -e "  ${DIM}# Text mode — type at the 'You>' prompt (great for SSH demos)${NC}"
        echo ""
        cmd "cd ${BRIDGE_DIR}"
        cmd "source .venv/bin/activate"
        cmd "PIDOG_VOICE_PROVIDER=text python3 -m bridge"
        echo ""
        echo -e "  ${DIM}# Simulated hardware (dev machine, no PiDog connected):${NC}"
        cmd "PIDOG_SIMULATE=true PIDOG_VOICE_PROVIDER=text python3 -m bridge"
        ;;

    "local")
        VOSK_MODEL="${PIDOG_STT_MODEL:-${BRIDGE_DIR}/models/vosk-model-small-en-us-0.15}"
        TTS_MODEL="${PIDOG_TTS_MODEL:-${BRIDGE_DIR}/models/piper/en_US-ryan-low.onnx}"
        echo -e "  ${DIM}# Local voice — Vosk STT + Piper TTS (offline, no cloud needed)${NC}"
        echo ""
        cmd "cd ${BRIDGE_DIR}"
        cmd "source .venv/bin/activate"
        cmd "PIDOG_VOICE_PROVIDER=local \\"
        cmd "PIDOG_STT_MODEL=${VOSK_MODEL} \\"
        cmd "PIDOG_TTS_MODEL=${TTS_MODEL} \\"
        cmd "    python3 -m bridge"
        echo ""
        echo -e "  ${DIM}# Optional tuning:${NC}"
        cmd "PIDOG_SENSOR_INTERVAL=2.0 \\"
        cmd "PIDOG_OBSTACLE_ALERT_CM=15 \\"
        cmd "PIDOG_WS_CHAT_TIMEOUT=120.0 \\"
        cmd "PIDOG_THREAD_POOL_SIZE=6 \\"
        cmd "    python3 -m bridge"
        ;;

    "pipecat-dc")
        echo -e "  ${DIM}# Deepgram STT + Cartesia TTS — low-latency cloud voice${NC}"
        echo -e "  ${DIM}# ~300ms STT + ~200ms TTS — best choice for video demos${NC}"
        echo ""
        cmd "cd ${BRIDGE_DIR}"
        cmd "source .venv/bin/activate"
        cmd "PIDOG_VOICE_PROVIDER=pipecat \\"
        cmd "PIPECAT_STT_PROVIDER=deepgram \\"
        cmd "PIPECAT_STT_API_KEY=\${PIPECAT_STT_API_KEY:-your-deepgram-key} \\"
        cmd "PIPECAT_TTS_PROVIDER=cartesia \\"
        cmd "PIPECAT_TTS_API_KEY=\${PIPECAT_TTS_API_KEY:-your-cartesia-key} \\"
        if [[ -n "${PIPECAT_TTS_VOICE:-}" ]]; then
            cmd "PIPECAT_TTS_VOICE=${PIPECAT_TTS_VOICE} \\"
        fi
        cmd "    python3 -m bridge"
        ;;

    "pipecat-de")
        echo -e "  ${DIM}# Deepgram STT + ElevenLabs TTS — voice cloning${NC}"
        echo -e "  ${DIM}# ~300ms STT + ~500ms TTS — best for custom dog voices${NC}"
        echo ""
        cmd "cd ${BRIDGE_DIR}"
        cmd "source .venv/bin/activate"
        cmd "PIDOG_VOICE_PROVIDER=pipecat \\"
        cmd "PIPECAT_STT_PROVIDER=deepgram \\"
        cmd "PIPECAT_STT_API_KEY=\${PIPECAT_STT_API_KEY:-your-deepgram-key} \\"
        cmd "PIPECAT_TTS_PROVIDER=elevenlabs \\"
        cmd "PIPECAT_TTS_API_KEY=\${PIPECAT_TTS_API_KEY:-your-elevenlabs-key} \\"
        if [[ -n "${PIPECAT_TTS_VOICE:-}" ]]; then
            cmd "PIPECAT_TTS_VOICE=${PIPECAT_TTS_VOICE} \\"
        fi
        cmd "    python3 -m bridge"
        ;;
esac

echo ""
echo -e "  ${DIM}# Stop the bridge: Ctrl+C or kill -SIGTERM <pid>${NC}"
echo -e "  ${DIM}# The bridge performs graceful shutdown: stand, LEDs off, close WS${NC}"

# TOML config alternative
echo ""
echo -e "  ${BOLD}Alternative: TOML config file${NC}"
echo ""
info "Create ${BRIDGE_DIR}/bridge_config.toml:"
cat << 'TOML_EXAMPLE'

    [bridge_config.toml]
    ─────────────────────────────────────────────────
    zenii_url = "http://127.0.0.1:18981"
    simulate_hardware = false
    thread_pool_size = 6

    [voice]
    provider = "pipecat"          # or "local" or "text"

    [voice.pipecat]
    stt_provider = "deepgram"
    tts_provider = "cartesia"     # or "elevenlabs"
    stt_api_key = "your-deepgram-key"
    tts_api_key = "your-cartesia-key"
    tts_voice = "your-voice-id"
    ─────────────────────────────────────────────────

TOML_EXAMPLE
cmd "export PIDOG_CONFIG=${BRIDGE_DIR}/bridge_config.toml"
cmd "python3 -m bridge"

# =============================================================================
# [6] Systemd Service Status
# =============================================================================
header "6" "Systemd Service"
echo ""

if command -v systemctl &>/dev/null; then
    BRIDGE_STATUS=$(systemctl is-active pidog-bridge 2>/dev/null || echo "inactive")
    case "${BRIDGE_STATUS}" in
        "active")   pass "pidog-bridge service: ${GREEN}active${NC}" ;;
        "inactive") info "pidog-bridge service: inactive (not started)" ;;
        "failed")   fail "pidog-bridge service: failed — check logs" ;;
        *)          info "pidog-bridge service: ${BRIDGE_STATUS}" ;;
    esac

    echo ""
    echo -e "  ${BOLD}Systemd commands:${NC}"
    cmd "sudo systemctl start   pidog-bridge   # start now"
    cmd "sudo systemctl stop    pidog-bridge   # stop"
    cmd "sudo systemctl restart pidog-bridge   # restart"
    cmd "sudo systemctl enable  pidog-bridge   # auto-start on boot"
    cmd "sudo systemctl status  pidog-bridge   # full status"
    cmd "journalctl -u pidog-bridge -f         # live logs"
    echo ""
    info "To update the voice provider in the service, edit the service file:"
    cmd "sudo systemctl edit pidog-bridge"
    echo -e "  ${DIM}Add or change: Environment=PIDOG_VOICE_PROVIDER=pipecat${NC}"

    # Update service with selected provider (show only, don't exec)
    echo ""
    echo -e "  ${BOLD}Service config for ${VOICE_PROVIDER}:${NC}"
    echo ""
    case "${VOICE_PROVIDER}" in
        "text")
            echo -e "  ${DIM}Environment=PIDOG_VOICE_PROVIDER=text${NC}"
            ;;
        "local")
            VOSK_MODEL="${PIDOG_STT_MODEL:-${BRIDGE_DIR}/models/vosk-model-small-en-us-0.15}"
            TTS_MODEL="${PIDOG_TTS_MODEL:-${BRIDGE_DIR}/models/piper/en_US-ryan-low.onnx}"
            echo -e "  ${DIM}Environment=PIDOG_VOICE_PROVIDER=local${NC}"
            echo -e "  ${DIM}Environment=PIDOG_STT_MODEL=${VOSK_MODEL}${NC}"
            echo -e "  ${DIM}Environment=PIDOG_TTS_MODEL=${TTS_MODEL}${NC}"
            ;;
        "pipecat-dc")
            echo -e "  ${DIM}Environment=PIDOG_VOICE_PROVIDER=pipecat${NC}"
            echo -e "  ${DIM}Environment=PIPECAT_STT_PROVIDER=deepgram${NC}"
            echo -e "  ${DIM}Environment=PIPECAT_TTS_PROVIDER=cartesia${NC}"
            echo -e "  ${DIM}# API keys are in Zenii credential store (no plaintext in service file)${NC}"
            ;;
        "pipecat-de")
            echo -e "  ${DIM}Environment=PIDOG_VOICE_PROVIDER=pipecat${NC}"
            echo -e "  ${DIM}Environment=PIPECAT_STT_PROVIDER=deepgram${NC}"
            echo -e "  ${DIM}Environment=PIPECAT_TTS_PROVIDER=elevenlabs${NC}"
            echo -e "  ${DIM}# API keys are in Zenii credential store (no plaintext in service file)${NC}"
            ;;
    esac
else
    skip "systemctl not found — systemd service management not available"
    info "Start bridge manually: cd ${BRIDGE_DIR} && source .venv/bin/activate && python3 -m bridge"
fi

# =============================================================================
# [7] Troubleshooting Cheatsheet
# =============================================================================
header "7" "Troubleshooting Cheatsheet"
echo ""
echo -e "  ${BOLD}Issue → Fix${NC}"
divider
echo ""
echo -e "  ${RED}Daemon not ready${NC} (bridge loops on startup)"
echo -e "     ${DIM}→ sudo systemctl start zenii-pidog${NC}"
echo ""
echo -e "  ${RED}WS connect failed${NC} (auth error)"
echo -e "     ${DIM}→ Check ZENII_TOKEN matches daemon config${NC}"
echo -e "     ${DIM}→ Check ~/.config/zenii/config.toml for auth_token field${NC}"
echo ""
echo -e "  ${RED}Microphone read failed${NC}"
echo -e "     ${DIM}→ arecord -l  (list available devices)${NC}"
echo -e "     ${DIM}→ Set ALSA_CARD=1 or use ~/.asoundrc to select device${NC}"
echo ""
echo -e "  ${RED}TTS binary not found${NC} (local mode)"
echo -e "     ${DIM}→ Install Piper: see step [3] above${NC}"
echo ""
echo -e "  ${RED}pidog library not found${NC}"
echo -e "     ${DIM}→ pip install pidog  (or ensure SunFounder lib is installed)${NC}"
echo -e "     ${DIM}→ Use PIDOG_SIMULATE=true on non-PiDog hardware${NC}"
echo ""
echo -e "  ${RED}aiohttp not installed${NC} (cloud voice REST calls fail)"
echo -e "     ${DIM}→ .venv/bin/pip install -r ${BRIDGE_DIR}/bridge/requirements.txt${NC}"
echo ""
echo -e "  ${RED}Action queue full${NC}"
echo -e "     ${DIM}→ Increase PIDOG_ACTION_TIMEOUT (default 10.0s)${NC}"
echo -e "     ${DIM}→ Check servo response time with: PIDOG_SIMULATE=true python3 -m bridge${NC}"
echo ""
echo -e "  ${RED}Sensor read timed out${NC}"
echo -e "     ${DIM}→ Increase PIDOG_SENSOR_READ_TIMEOUT to 10.0 (default 5.0s)${NC}"
echo ""
echo -e "  ${BOLD}Debug logging:${NC}"
cmd "PYTHONUNBUFFERED=1 python3 -m bridge 2>&1 | tee bridge.log"
echo -e "  ${DIM}For DEBUG level: edit bridge/__main__.py → level=logging.DEBUG${NC}"
echo ""

# =============================================================================
# Results
# =============================================================================
divider
TOTAL=$((PASS + FAIL + SKIP))
echo ""
echo -e "${BOLD}  Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}, ${YELLOW}${SKIP} skipped${NC} (${TOTAL} total)"
echo ""

if [[ "${FAIL}" -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}  All checks passed! Bridge is ready to run.${NC}"
    echo ""
    echo -e "  ${BOLD}Next step:${NC} Run the bridge with the command shown in section [5]."
    echo -e "  ${BOLD}Then run:${NC}  bash 03_capabilities_test.sh  (full feature demo)"
else
    echo -e "${YELLOW}${BOLD}  Some checks failed — fix the issues above before recording.${NC}"
fi

echo ""
echo -e "  ${DIM}Voice provider:    ${VOICE_PROVIDER}${NC}"
echo -e "  ${DIM}Bridge directory:  ${BRIDGE_DIR}${NC}"
echo -e "  ${DIM}Daemon URL:        ${ZENII_URL}${NC}"
echo ""
echo -e "  ${CYAN}${BOLD}PiDog is the body. Zenii is the brain.${NC}"
echo ""
