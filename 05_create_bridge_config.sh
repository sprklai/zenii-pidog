#!/usr/bin/env bash
# =============================================================================
# Zenii PiDog2 — Create bridge_config.toml
#
# Generates ~/zenii-pidog/bridge_config.toml (or $BRIDGE_DIR) with
# real values pulled from the Zenii credential store + current environment.
# Covers all three voice providers: text, local, pipecat (Deepgram+Cartesia/ElevenLabs).
#
# Usage:
#   bash 05_create_bridge_config.sh
#
# Prerequisites:
#   - 01_pidog_setup_script.sh has been run (bridge installed)
#   - Daemon is running (for credential lookups)
#   - API keys stored via step [2] of 04_bridge_usage_examples.sh
#     OR set as env vars: PIPECAT_STT_API_KEY, PIPECAT_TTS_API_KEY, PIPECAT_TTS_VOICE
# =============================================================================
set -uo pipefail

# --- Config (override via environment) ---
ZENII_URL="${ZENII_URL:-http://127.0.0.1:18981}"
BRIDGE_DIR="${BRIDGE_DIR:-${HOME}/zenii-pidog}"
ZENII_TOKEN="${ZENII_TOKEN:-}"
CONFIG_PATH="${BRIDGE_DIR}/bridge_config.toml"
CURL_TIMEOUT=10

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

pass()    { echo -e "  ${GREEN}PASS${NC}  $*"; }
fail()    { echo -e "  ${RED}FAIL${NC}  $*"; }
skip()    { echo -e "  ${YELLOW}SKIP${NC}  $*"; }
info()    { echo -e "  ${BLUE}[INFO]${NC} $*"; }
warn()    { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
cmd()     { echo -e "  ${DIM}\$${NC} $*"; }

_auth_header() {
    if [[ -n "${ZENII_TOKEN}" ]]; then
        echo "-H" "Authorization: Bearer ${ZENII_TOKEN}"
    fi
}

api() { curl -sf --max-time "${CURL_TIMEOUT}" "$(_auth_header)" "$@" 2>&1; }

# Fetch a credential value from Zenii store; returns empty string if not found
get_cred() {
    local key="$1"
    local resp
    resp=$(curl -sf --max-time "${CURL_TIMEOUT}" \
        ${ZENII_TOKEN:+-H "Authorization: Bearer ${ZENII_TOKEN}"} \
        "${ZENII_URL}/credentials/${key}" 2>/dev/null || true)
    # Daemon intentionally never returns raw values — check if key exists via list
    echo ""
}

# Check if a credential key exists in the store
cred_exists() {
    local key="$1"
    local creds
    creds=$(curl -sf --max-time "${CURL_TIMEOUT}" \
        ${ZENII_TOKEN:+-H "Authorization: Bearer ${ZENII_TOKEN}"} \
        "${ZENII_URL}/credentials" 2>/dev/null || echo "{}")
    echo "${creds}" | grep -q "\"${key}\""
}

# =============================================================================
# Banner
# =============================================================================
echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║   Zenii PiDog2 — Create bridge_config.toml           ║"
echo "  ║   Generates TOML config from env + credential store  ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo -e "${NC}"

# =============================================================================
# Preflight
# =============================================================================
echo -e "${CYAN}${BOLD}[0]${NC} ${BOLD}Preflight${NC}"
echo ""

if [[ ! -d "${BRIDGE_DIR}" ]]; then
    fail "Bridge dir not found: ${BRIDGE_DIR}"
    info "Run 01_pidog_setup_script.sh first, or set: export BRIDGE_DIR=/path/to/zenii-pidog"
    exit 1
fi
pass "Bridge dir: ${BRIDGE_DIR}"

DAEMON_UP=false
if curl -sf --max-time 5 "${ZENII_URL}/health" &>/dev/null; then
    DAEMON_UP=true
    pass "Daemon reachable at ${ZENII_URL}"
else
    warn "Daemon not reachable — credential store lookups will be skipped"
    warn "API keys must come from environment variables instead"
fi

echo ""

# =============================================================================
# Voice provider selection
# =============================================================================
echo -e "${CYAN}${BOLD}[1]${NC} ${BOLD}Voice Provider${NC}"
echo ""
echo -e "  ${BOLD}Select voice provider for bridge_config.toml:${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} ${BOLD}text${NC}        — stdin/stdout (SSH, dev)"
echo -e "  ${CYAN}2)${NC} ${BOLD}local${NC}       — Vosk STT + Piper TTS (offline)"
echo -e "  ${CYAN}3)${NC} ${BOLD}pipecat-dc${NC}  — Deepgram + Cartesia (cloud, low-latency)"
echo -e "  ${CYAN}4)${NC} ${BOLD}pipecat-de${NC}  — Deepgram + ElevenLabs (cloud, voice cloning)"
echo ""
read -rp "  Select [1-4]: " CHOICE

case "${CHOICE}" in
    1) VOICE_PROVIDER="text"       ;;
    2) VOICE_PROVIDER="local"      ;;
    3) VOICE_PROVIDER="pipecat-dc" ;;
    4) VOICE_PROVIDER="pipecat-de" ;;
    *)
        warn "Invalid choice — defaulting to text"
        VOICE_PROVIDER="text"
        ;;
esac

echo ""
pass "Voice provider: ${VOICE_PROVIDER}"

# =============================================================================
# Collect API keys (pipecat providers only)
# =============================================================================
STT_API_KEY=""
TTS_API_KEY=""
TTS_VOICE=""
STT_PROVIDER=""
TTS_PROVIDER=""

if [[ "${VOICE_PROVIDER}" == pipecat-* ]]; then
    echo ""
    echo -e "${CYAN}${BOLD}[2]${NC} ${BOLD}API Keys${NC}"
    echo ""

    STT_PROVIDER="deepgram"
    TTS_PROVIDER="cartesia"
    [[ "${VOICE_PROVIDER}" == "pipecat-de" ]] && TTS_PROVIDER="elevenlabs"

    # --- Deepgram ---
    if [[ -n "${PIPECAT_STT_API_KEY:-}" ]]; then
        STT_API_KEY="${PIPECAT_STT_API_KEY}"
        pass "Deepgram key: from environment (PIPECAT_STT_API_KEY)"
    else
        if "${DAEMON_UP}" && cred_exists "api_key:deepgram"; then
            info "Deepgram key found in credential store (daemon cannot return raw values)"
        fi
        echo -n "  Enter Deepgram API key: "
        read -rs DG_KEY
        echo ""
        if [[ -n "${DG_KEY}" ]]; then
            STT_API_KEY="${DG_KEY}"
            pass "Deepgram key: entered"
        else
            STT_API_KEY=""
            warn "No Deepgram key entered — STT will fail until you edit bridge_config.toml"
        fi
    fi

    echo ""

    # --- Cartesia or ElevenLabs ---
    CRED_KEY="api_key:${TTS_PROVIDER}"
    TTS_DISPLAY="${TTS_PROVIDER^}"

    if [[ -n "${PIPECAT_TTS_API_KEY:-}" ]]; then
        TTS_API_KEY="${PIPECAT_TTS_API_KEY}"
        pass "${TTS_DISPLAY} key: from environment (PIPECAT_TTS_API_KEY)"
    else
        if "${DAEMON_UP}" && cred_exists "${CRED_KEY}"; then
            info "${TTS_DISPLAY} key found in credential store (daemon cannot return raw values)"
        fi
        echo -n "  Enter ${TTS_DISPLAY} API key: "
        read -rs TS_KEY
        echo ""
        if [[ -n "${TS_KEY}" ]]; then
            TTS_API_KEY="${TS_KEY}"
            pass "${TTS_DISPLAY} key: entered"
        else
            TTS_API_KEY=""
            warn "No ${TTS_DISPLAY} key entered — TTS will fail until you edit bridge_config.toml"
        fi
    fi

    echo ""

    # --- TTS Voice ID ---
    if [[ -n "${PIPECAT_TTS_VOICE:-}" ]]; then
        TTS_VOICE="${PIPECAT_TTS_VOICE}"
        pass "TTS voice ID: from environment"
    else
        if [[ "${TTS_PROVIDER}" == "cartesia" ]]; then
            DEFAULT_VOICE="a0e99841-438c-4a64-b679-ae501e7d6091"
            echo -e "  ${DIM}Cartesia voice ID — find at play.cartesia.ai/voices${NC}"
            echo -n "  Enter voice ID [default: ${DEFAULT_VOICE} (Sonic English)]: "
        else
            DEFAULT_VOICE=""
            echo -e "  ${DIM}ElevenLabs voice ID — find at elevenlabs.io/voice-library${NC}"
            echo -n "  Enter voice ID [leave blank to use account default]: "
        fi
        read -r V_ID
        if [[ -n "${V_ID}" ]]; then
            TTS_VOICE="${V_ID}"
            pass "TTS voice ID: ${TTS_VOICE}"
        elif [[ -n "${DEFAULT_VOICE}" ]]; then
            TTS_VOICE="${DEFAULT_VOICE}"
            pass "TTS voice ID: ${TTS_VOICE} (default)"
        fi
    fi
fi

# =============================================================================
# Hardware options
# =============================================================================
echo ""
echo -e "${CYAN}${BOLD}[3]${NC} ${BOLD}Hardware Mode${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} ${BOLD}real${NC}      — physical PiDog2 connected"
echo -e "  ${CYAN}2)${NC} ${BOLD}simulate${NC}  — simulated (dev machine, no PiDog)"
echo ""
read -rp "  Select [1-2, default=1]: " HW_CHOICE

case "${HW_CHOICE}" in
    2) SIMULATE="true"  ;;
    *) SIMULATE="false" ;;
esac

pass "Hardware: simulate_hardware = ${SIMULATE}"

# =============================================================================
# Local voice model paths (local provider only)
# =============================================================================
VOSK_MODEL_PATH=""
PIPER_MODEL_PATH=""
PIPER_BINARY="piper"

if [[ "${VOICE_PROVIDER}" == "local" ]]; then
    echo ""
    echo -e "${CYAN}${BOLD}[4]${NC} ${BOLD}Local Model Paths${NC}"
    echo ""

    DEFAULT_VOSK="${BRIDGE_DIR}/models/vosk-model-small-en-us-0.15"
    DEFAULT_PIPER="${BRIDGE_DIR}/models/piper/en_US-ryan-low.onnx"

    echo -n "  Vosk STT model path [default: ${DEFAULT_VOSK}]: "
    read -r VOSK_INPUT
    VOSK_MODEL_PATH="${VOSK_INPUT:-${DEFAULT_VOSK}}"

    echo -n "  Piper TTS model path [default: ${DEFAULT_PIPER}]: "
    read -r PIPER_INPUT
    PIPER_MODEL_PATH="${PIPER_INPUT:-${DEFAULT_PIPER}}"

    echo -n "  Piper binary path [default: piper]: "
    read -r BIN_INPUT
    PIPER_BINARY="${BIN_INPUT:-piper}"

    pass "Vosk model: ${VOSK_MODEL_PATH}"
    pass "Piper model: ${PIPER_MODEL_PATH}"
    pass "Piper binary: ${PIPER_BINARY}"
fi

# =============================================================================
# AI Provider (configures the Zenii daemon's LLM at bridge startup)
# =============================================================================
echo ""
echo -e "${CYAN}${BOLD}[5]${NC} ${BOLD}AI Provider${NC}"
echo ""
echo -e "  ${BOLD}Which AI provider should the PiDog use?${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} ${BOLD}anthropic${NC}   — Claude (claude-sonnet-4-6, claude-haiku-4-5-...)"
echo -e "  ${CYAN}2)${NC} ${BOLD}openai${NC}      — GPT-4o, GPT-4o-mini"
echo -e "  ${CYAN}3)${NC} ${BOLD}ollama${NC}      — Local models (llama3, mistral, etc.)"
echo -e "  ${CYAN}4)${NC} ${BOLD}skip${NC}        — Don't configure AI (use existing Zenii settings)"
echo ""
read -rp "  Select [1-4, default=4]: " AI_CHOICE

AI_PROVIDER=""
AI_MODEL=""
AI_API_KEY=""

case "${AI_CHOICE}" in
    1)
        AI_PROVIDER="anthropic"
        DEFAULT_MODEL="claude-sonnet-4-6"
        echo -n "  Model [default: ${DEFAULT_MODEL}]: "
        read -r AI_MODEL_INPUT
        AI_MODEL="${AI_MODEL_INPUT:-${DEFAULT_MODEL}}"
        echo ""
        echo -n "  Anthropic API key (sk-ant-...): "
        read -rs AI_API_KEY
        echo ""
        if [[ -n "${AI_API_KEY}" ]]; then
            pass "Anthropic: model=${AI_MODEL}, key entered"
        else
            warn "No API key entered — set ZENII_AI_API_KEY or edit config manually"
        fi
        ;;
    2)
        AI_PROVIDER="openai"
        DEFAULT_MODEL="gpt-4o"
        echo -n "  Model [default: ${DEFAULT_MODEL}]: "
        read -r AI_MODEL_INPUT
        AI_MODEL="${AI_MODEL_INPUT:-${DEFAULT_MODEL}}"
        echo ""
        echo -n "  OpenAI API key (sk-...): "
        read -rs AI_API_KEY
        echo ""
        if [[ -n "${AI_API_KEY}" ]]; then
            pass "OpenAI: model=${AI_MODEL}, key entered"
        else
            warn "No API key entered — set ZENII_AI_API_KEY or edit config manually"
        fi
        ;;
    3)
        AI_PROVIDER="ollama"
        DEFAULT_MODEL="llama3"
        echo -n "  Model [default: ${DEFAULT_MODEL}]: "
        read -r AI_MODEL_INPUT
        AI_MODEL="${AI_MODEL_INPUT:-${DEFAULT_MODEL}}"
        AI_API_KEY=""
        pass "Ollama: model=${AI_MODEL} (no key needed)"
        ;;
    *)
        skip "AI provider: skipped (using existing Zenii settings)"
        ;;
esac

# =============================================================================
# Write bridge_config.toml
# =============================================================================
echo ""
echo -e "${CYAN}${BOLD}[6]${NC} ${BOLD}Writing Config${NC}"
echo ""

# Back up existing config if present
if [[ -f "${CONFIG_PATH}" ]]; then
    BACKUP="${CONFIG_PATH}.bak.$(date +%Y%m%d_%H%M%S)"
    cp "${CONFIG_PATH}" "${BACKUP}"
    info "Backed up existing config to: ${BACKUP}"
fi

# Determine voice provider string for TOML
case "${VOICE_PROVIDER}" in
    "text")       TOML_PROVIDER="text"   ;;
    "local")      TOML_PROVIDER="local"  ;;
    "pipecat-dc") TOML_PROVIDER="pipecat" ;;
    "pipecat-de") TOML_PROVIDER="pipecat" ;;
    *)            TOML_PROVIDER="text"   ;;
esac

# Build TOML
{
    echo "# bridge_config.toml — Zenii PiDog2 bridge configuration"
    echo "# Generated by 05_create_bridge_config.sh on $(date)"
    echo "# Docs: go2market/pidog/bridge/README.md"
    echo ""
    echo "simulate_hardware = ${SIMULATE}"
    echo "thread_pool_size = 6"
    echo ""
    echo "[zenii]"
    echo "url = \"${ZENII_URL}\""
    if [[ -n "${ZENII_TOKEN}" ]]; then
        echo "token = \"${ZENII_TOKEN}\""
    fi
    if [[ -n "${AI_PROVIDER}" ]]; then
        echo "ai_provider = \"${AI_PROVIDER}\""
        echo "ai_model = \"${AI_MODEL}\""
        if [[ -n "${AI_API_KEY}" ]]; then
            echo "ai_api_key = \"${AI_API_KEY}\""
        fi
    fi
    echo ""
    echo "[voice]"
    echo "provider = \"${TOML_PROVIDER}\""

    if [[ "${VOICE_PROVIDER}" == "local" ]]; then
        echo ""
        echo "[voice.local]"
        echo "stt_model = \"${VOSK_MODEL_PATH}\""
        echo "tts_model = \"${PIPER_MODEL_PATH}\""
        echo "tts_binary = \"${PIPER_BINARY}\""
    fi

    if [[ "${VOICE_PROVIDER}" == pipecat-* ]]; then
        echo ""
        echo "[voice.pipecat]"
        echo "stt_provider = \"${STT_PROVIDER}\""
        echo "tts_provider = \"${TTS_PROVIDER}\""
        echo "stt_api_key = \"${STT_API_KEY}\""
        echo "tts_api_key = \"${TTS_API_KEY}\""
        if [[ -n "${TTS_VOICE}" ]]; then
            echo "tts_voice = \"${TTS_VOICE}\""
        fi
    fi

    echo ""
    echo "# Optional tuning (uncomment to override defaults)"
    echo "# mic_device = -1                 # sounddevice input index; -1 = system default"
    echo "#                                 # PiDog I2S mic: run 'arecord -l' to find device index"
    echo "# speaker_device = -1             # sounddevice output index; -1 = system default"
    echo "# listen_timeout_secs = 5.0       # recording window per utterance (seconds)"
    echo "# silence_threshold = 0.3         # 0.0-1.0 fraction of int16 max; filters ambient noise"
    echo "# sensor_interval_secs = 2.0"
    echo "# memory_throttle_secs = 30.0"
    echo "# obstacle_alert_cm = 15"
    echo "# touch_cooldown_secs = 60.0"
    echo "# ws_chat_timeout_secs = 120.0"
    echo "# action_timeout_secs = 10.0"
    echo "# sensor_read_timeout_secs = 5.0"
    echo "# default_action_speed = 80"

} > "${CONFIG_PATH}"

pass "Config written: ${CONFIG_PATH}"
echo ""

# Show the generated file
echo -e "  ${BOLD}Generated ${CONFIG_PATH}:${NC}"
echo ""
sed 's/^/    /' "${CONFIG_PATH}"
echo ""

# =============================================================================
# Run instructions
# =============================================================================
echo -e "${CYAN}${BOLD}[7]${NC} ${BOLD}Run${NC}"
echo ""
echo -e "  ${BOLD}Start the bridge with this config:${NC}"
echo ""
echo -e "  ${DIM}\$${NC} export PIDOG_CONFIG=${CONFIG_PATH}"
echo -e "  ${DIM}\$${NC} cd ${BRIDGE_DIR}"
echo -e "  ${DIM}\$${NC} source .venv/bin/activate"
echo -e "  ${DIM}\$${NC} python3 -m bridge"
echo ""

if [[ -f "/etc/systemd/system/pidog-bridge.service" ]]; then
    echo -e "  ${BOLD}Or update your systemd unit to use the config file:${NC}"
    echo ""
    echo -e "  ${DIM}# In /etc/systemd/system/pidog-bridge.service, under [Service]:${NC}"
    echo -e "  ${DIM}Environment=PIDOG_CONFIG=${CONFIG_PATH}${NC}"
    echo ""
    echo -e "  ${DIM}\$${NC} sudo systemctl daemon-reload"
    echo -e "  ${DIM}\$${NC} sudo systemctl restart pidog-bridge"
    echo ""
fi

echo -e "  ${DIM}# Stop: Ctrl+C or kill -SIGTERM <pid>${NC}"
echo ""
