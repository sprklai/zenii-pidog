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
echo -e "  ${CYAN}1)${NC} ${BOLD}text${NC}     — stdin/stdout (SSH, dev)"
echo -e "  ${CYAN}2)${NC} ${BOLD}local${NC}    — Vosk STT + Piper TTS (offline)"
echo -e "  ${CYAN}3)${NC} ${BOLD}pipecat${NC}  — cloud STT + cloud TTS (pick providers next)"
echo ""
read -rp "  Select [1-3]: " CHOICE

case "${CHOICE}" in
    1) VOICE_PROVIDER="text"    ;;
    2) VOICE_PROVIDER="local"   ;;
    3) VOICE_PROVIDER="pipecat" ;;
    *)
        warn "Invalid choice — defaulting to text"
        VOICE_PROVIDER="text"
        ;;
esac

echo ""
pass "Voice provider: ${VOICE_PROVIDER}"

# =============================================================================
# Collect STT + TTS provider, model, voice, API keys (pipecat only)
# STT and TTS are chosen independently — any STT can pair with any TTS.
# =============================================================================
STT_PROVIDER=""
STT_API_KEY=""
STT_MODEL=""
SARVAM_LANGUAGE=""
TTS_PROVIDER=""
TTS_API_KEY=""
TTS_MODEL=""
TTS_VOICE=""

if [[ "${VOICE_PROVIDER}" == "pipecat" ]]; then
    # ---- [2] STT provider ----
    echo ""
    echo -e "${CYAN}${BOLD}[2]${NC} ${BOLD}STT Provider${NC}"
    echo ""
    echo -e "  ${CYAN}1)${NC} ${BOLD}deepgram${NC}  — Nova-2 WebSocket streaming (low latency, built-in VAD)"
    echo -e "  ${CYAN}2)${NC} ${BOLD}sarvam${NC}    — Saaras streaming (Indian English + 10 Indian languages)"
    echo -e "  ${CYAN}3)${NC} ${BOLD}groq${NC}      — Whisper large-v3-turbo (batch)"
    echo -e "  ${CYAN}4)${NC} ${BOLD}azure${NC}     — Azure Speech (batch)"
    echo -e "  ${CYAN}5)${NC} ${BOLD}google${NC}    — Google Cloud Speech (batch)"
    echo ""
    read -rp "  Select [1-5]: " STT_CHOICE

    case "${STT_CHOICE}" in
        1) STT_PROVIDER="deepgram"; DEFAULT_STT_MODEL="nova-2"                 ;;
        2) STT_PROVIDER="sarvam";   DEFAULT_STT_MODEL="saaras:v3"              ;;
        3) STT_PROVIDER="groq";     DEFAULT_STT_MODEL="whisper-large-v3-turbo" ;;
        4) STT_PROVIDER="azure";    DEFAULT_STT_MODEL="eastus"                 ;;
        5) STT_PROVIDER="google";   DEFAULT_STT_MODEL=""                       ;;
        *)
            warn "Invalid choice — defaulting to deepgram"
            STT_PROVIDER="deepgram"; DEFAULT_STT_MODEL="nova-2"
            ;;
    esac
    STT_DISPLAY="${STT_PROVIDER^}"
    pass "STT provider: ${STT_PROVIDER}"

    # STT model. Required for Sarvam (code refuses to run without it);
    # optional-with-defaults for others.
    if [[ -n "${PIPECAT_STT_MODEL:-}" ]]; then
        STT_MODEL="${PIPECAT_STT_MODEL}"
        pass "STT model: from environment (${STT_MODEL})"
    else
        if [[ -n "${DEFAULT_STT_MODEL}" ]]; then
            echo -n "  ${STT_DISPLAY} model [default: ${DEFAULT_STT_MODEL}]: "
        else
            echo -n "  ${STT_DISPLAY} model [leave blank to use provider default]: "
        fi
        read -r STT_MODEL_INPUT
        STT_MODEL="${STT_MODEL_INPUT:-${DEFAULT_STT_MODEL}}"
        [[ -n "${STT_MODEL}" ]] && pass "STT model: ${STT_MODEL}"
    fi

    # STT API key
    if [[ -n "${PIPECAT_STT_API_KEY:-}" ]]; then
        STT_API_KEY="${PIPECAT_STT_API_KEY}"
        pass "${STT_DISPLAY} key: from environment (PIPECAT_STT_API_KEY)"
    else
        if "${DAEMON_UP}" && cred_exists "api_key:${STT_PROVIDER}"; then
            info "${STT_DISPLAY} key found in credential store (daemon cannot return raw values)"
        fi
        echo -n "  Enter ${STT_DISPLAY} API key: "
        read -rs STT_KEY_INPUT
        echo ""
        if [[ -n "${STT_KEY_INPUT}" ]]; then
            STT_API_KEY="${STT_KEY_INPUT}"
            pass "${STT_DISPLAY} key: entered"
        else
            warn "No ${STT_DISPLAY} key entered — STT will fail until you edit bridge_config.toml"
        fi
    fi

    # Sarvam-specific language
    if [[ "${STT_PROVIDER}" == "sarvam" ]]; then
        echo ""
        echo -e "  ${BOLD}Sarvam AI language code${NC}"
        echo -e "  ${DIM}Options: en-IN, hi-IN, ta-IN, te-IN, kn-IN, ml-IN, mr-IN, gu-IN, pa-IN, bn-IN${NC}"
        if [[ -n "${SARVAM_LANGUAGE_CODE:-}" ]]; then
            SARVAM_LANGUAGE="${SARVAM_LANGUAGE_CODE}"
            pass "Sarvam language: from environment (${SARVAM_LANGUAGE})"
        else
            read -rp "  Language code [default: en-IN]: " LANG_INPUT
            SARVAM_LANGUAGE="${LANG_INPUT:-en-IN}"
            pass "Sarvam language: ${SARVAM_LANGUAGE}"
        fi
    fi

    # ---- [3] TTS provider ----
    echo ""
    echo -e "${CYAN}${BOLD}[3]${NC} ${BOLD}TTS Provider${NC}"
    echo ""
    echo -e "  ${CYAN}1)${NC} ${BOLD}cartesia${NC}    — Cartesia Sonic (streaming, low-latency)"
    echo -e "  ${CYAN}2)${NC} ${BOLD}elevenlabs${NC}  — ElevenLabs (voice cloning)"
    echo -e "  ${CYAN}3)${NC} ${BOLD}azure${NC}       — Azure Neural TTS"
    echo -e "  ${CYAN}4)${NC} ${BOLD}google${NC}      — Google Cloud TTS"
    echo ""
    read -rp "  Select [1-4]: " TTS_CHOICE

    case "${TTS_CHOICE}" in
        1) TTS_PROVIDER="cartesia";   DEFAULT_TTS_MODEL="sonic-english";         DEFAULT_TTS_VOICE="a0e99841-438c-4a64-b679-ae501e7d6091" ;;
        2) TTS_PROVIDER="elevenlabs"; DEFAULT_TTS_MODEL="eleven_monolingual_v1"; DEFAULT_TTS_VOICE="" ;;
        3) TTS_PROVIDER="azure";      DEFAULT_TTS_MODEL="eastus";                DEFAULT_TTS_VOICE="en-US-JennyNeural" ;;
        4) TTS_PROVIDER="google";     DEFAULT_TTS_MODEL="";                      DEFAULT_TTS_VOICE="en-US-Standard-C" ;;
        *)
            warn "Invalid choice — defaulting to cartesia"
            TTS_PROVIDER="cartesia"; DEFAULT_TTS_MODEL="sonic-english"; DEFAULT_TTS_VOICE="a0e99841-438c-4a64-b679-ae501e7d6091"
            ;;
    esac
    TTS_DISPLAY="${TTS_PROVIDER^}"
    pass "TTS provider: ${TTS_PROVIDER}"

    # TTS model (Azure: region. Cartesia/ElevenLabs: model id. Google: unused.)
    if [[ -n "${PIPECAT_TTS_MODEL:-}" ]]; then
        TTS_MODEL="${PIPECAT_TTS_MODEL}"
        pass "TTS model: from environment (${TTS_MODEL})"
    else
        if [[ -n "${DEFAULT_TTS_MODEL}" ]]; then
            echo -n "  ${TTS_DISPLAY} model [default: ${DEFAULT_TTS_MODEL}]: "
        else
            echo -n "  ${TTS_DISPLAY} model [leave blank to use provider default]: "
        fi
        read -r TTS_MODEL_INPUT
        TTS_MODEL="${TTS_MODEL_INPUT:-${DEFAULT_TTS_MODEL}}"
        [[ -n "${TTS_MODEL}" ]] && pass "TTS model: ${TTS_MODEL}"
    fi

    # TTS API key
    if [[ -n "${PIPECAT_TTS_API_KEY:-}" ]]; then
        TTS_API_KEY="${PIPECAT_TTS_API_KEY}"
        pass "${TTS_DISPLAY} key: from environment (PIPECAT_TTS_API_KEY)"
    else
        if "${DAEMON_UP}" && cred_exists "api_key:${TTS_PROVIDER}"; then
            info "${TTS_DISPLAY} key found in credential store (daemon cannot return raw values)"
        fi
        echo -n "  Enter ${TTS_DISPLAY} API key: "
        read -rs TTS_KEY_INPUT
        echo ""
        if [[ -n "${TTS_KEY_INPUT}" ]]; then
            TTS_API_KEY="${TTS_KEY_INPUT}"
            pass "${TTS_DISPLAY} key: entered"
        else
            warn "No ${TTS_DISPLAY} key entered — TTS will fail until you edit bridge_config.toml"
        fi
    fi

    # TTS voice ID
    if [[ -n "${PIPECAT_TTS_VOICE:-}" ]]; then
        TTS_VOICE="${PIPECAT_TTS_VOICE}"
        pass "TTS voice: from environment (${TTS_VOICE})"
    else
        case "${TTS_PROVIDER}" in
            cartesia)
                echo -e "  ${DIM}Cartesia voice ID — find at play.cartesia.ai/voices${NC}"
                echo -n "  Voice ID [default: ${DEFAULT_TTS_VOICE} (Sonic English)]: "
                ;;
            elevenlabs)
                echo -e "  ${DIM}ElevenLabs voice ID — find at elevenlabs.io/voice-library${NC}"
                echo -n "  Voice ID [leave blank to use account default]: "
                ;;
            azure)
                echo -e "  ${DIM}Azure neural voice name (e.g. en-US-JennyNeural, en-IN-NeerjaNeural)${NC}"
                echo -n "  Voice name [default: ${DEFAULT_TTS_VOICE}]: "
                ;;
            google)
                echo -e "  ${DIM}Google Cloud TTS voice (e.g. en-US-Standard-C, en-IN-Wavenet-A)${NC}"
                echo -n "  Voice name [default: ${DEFAULT_TTS_VOICE}]: "
                ;;
        esac
        read -r TTS_VOICE_INPUT
        TTS_VOICE="${TTS_VOICE_INPUT:-${DEFAULT_TTS_VOICE}}"
        [[ -n "${TTS_VOICE}" ]] && pass "TTS voice: ${TTS_VOICE}"
    fi
fi

# =============================================================================
# Hardware options
# =============================================================================
echo ""
echo -e "${CYAN}${BOLD}[4]${NC} ${BOLD}Hardware Mode${NC}"
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
    echo -e "${CYAN}${BOLD}[5]${NC} ${BOLD}Local Model Paths${NC}"
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
echo -e "${CYAN}${BOLD}[6]${NC} ${BOLD}AI Provider${NC}"
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
echo -e "${CYAN}${BOLD}[7]${NC} ${BOLD}Writing Config${NC}"
echo ""

# Back up existing config if present
if [[ -f "${CONFIG_PATH}" ]]; then
    BACKUP="${CONFIG_PATH}.bak.$(date +%Y%m%d_%H%M%S)"
    cp "${CONFIG_PATH}" "${BACKUP}"
    info "Backed up existing config to: ${BACKUP}"
fi

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
    echo "provider = \"${VOICE_PROVIDER}\""

    if [[ "${VOICE_PROVIDER}" == "local" ]]; then
        echo ""
        echo "[voice.local]"
        echo "stt_model = \"${VOSK_MODEL_PATH}\""
        echo "tts_model = \"${PIPER_MODEL_PATH}\""
        echo "tts_binary = \"${PIPER_BINARY}\""
    fi

    if [[ "${VOICE_PROVIDER}" == "pipecat" ]]; then
        echo ""
        echo "[voice.pipecat]"
        echo "stt_provider = \"${STT_PROVIDER}\""
        echo "stt_api_key  = \"${STT_API_KEY}\""
        if [[ -n "${STT_MODEL}" ]]; then
            echo "stt_model    = \"${STT_MODEL}\""
        fi
        echo "tts_provider = \"${TTS_PROVIDER}\""
        echo "tts_api_key  = \"${TTS_API_KEY}\""
        if [[ -n "${TTS_MODEL}" ]]; then
            echo "tts_model    = \"${TTS_MODEL}\""
        fi
        if [[ -n "${TTS_VOICE}" ]]; then
            echo "tts_voice    = \"${TTS_VOICE}\""
        fi
        if [[ -n "${SARVAM_LANGUAGE}" ]]; then
            echo "sarvam_language_code = \"${SARVAM_LANGUAGE}\""
        fi
    fi

    echo ""
    echo "# Optional tuning (uncomment to override defaults)"
    echo "# mic_device = -1                 # sounddevice input index; -1 = system default"
    echo "#                                 # PiDog I2S mic: run 'arecord -l' to find device index"
    echo "# speaker_device = -1             # sounddevice output index; -1 = system default"
    echo "# listen_timeout_secs = 5.0       # recording window per utterance (seconds)"
    echo "# silence_threshold = 0.02        # 0.0-1.0 fraction of int16 max; raise (e.g. 0.05) in noisy rooms"
    echo "# sensor_interval_secs = 2.0"
    echo "# memory_throttle_secs = 30.0"
    echo "# obstacle_alert_cm = 15"
    echo "# touch_cooldown_secs = 60.0"
    echo "# ws_chat_timeout_secs = 120.0"
    echo "# action_timeout_secs = 10.0"
    echo "# sensor_read_timeout_secs = 5.0"
    echo "# default_action_speed = 80"
    echo ""
    echo "# LCD1602 display (optional — requires I2C wiring + smbus2)"
    echo "# pip install smbus2   # or: sudo apt install python3-smbus"
    echo "# Run 'i2cdetect -y 1' to confirm I2C address (usually 0x27 or 0x3F)"
    echo "# [lcd]"
    echo "# enabled = true"
    echo "# address = 0x27          # PCF8574T=0x27, PCF8574AT=0x3F"
    echo "# bus = 1                 # I2C bus (1 on all modern RPi models)"
    echo "# scroll_delay = 0.35     # seconds between scroll steps"

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
echo -e "${CYAN}${BOLD}[8]${NC} ${BOLD}Run${NC}"
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
