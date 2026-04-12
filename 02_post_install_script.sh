#!/usr/bin/env bash
# =============================================================================
# Zenii PiDog2 Post-Install Script
#
# Run AFTER pidog_setup_script.sh completes and the daemon is healthy.
# Interactively selects AI provider, stores API key, sets default model,
# tests chat, and demos personality swapping + memory persistence.
#
# Usage:
#   bash 02_post_install_script.sh
# =============================================================================
set -euo pipefail

ZENII_URL="http://127.0.0.1:18981"
PERSONAS_DIR="${HOME}/.local/share/zenii/personas"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${CYAN}${BOLD}==> $*${NC}"; }

# --- Dependency check ---
if ! command -v jq &>/dev/null; then
    echo -e "${RED}[ERROR]${NC} jq is required but not installed." >&2
    echo "  Install: sudo apt-get install -y jq"
    exit 1
fi

# =============================================================================
# Step 1: Health check
# =============================================================================
step "Checking daemon health"

if ! curl -sf --max-time 10 "${ZENII_URL}/health" &>/dev/null; then
    error "Daemon not responding at ${ZENII_URL}/health"
    error "Start it: sudo systemctl start zenii-pidog"
    exit 1
fi
ok "Daemon is healthy"

# =============================================================================
# Step 2: Select AI provider
# =============================================================================
step "Select your AI provider"

PROVIDERS_JSON=$(curl -sf --max-time 10 "${ZENII_URL}/providers" 2>/dev/null || echo "")

if [[ -z "${PROVIDERS_JSON}" ]]; then
    error "Failed to fetch providers from daemon"
    exit 1
fi

PROVIDER_COUNT=$(echo "${PROVIDERS_JSON}" | jq 'length')

echo ""
for i in $(seq 0 $((PROVIDER_COUNT - 1))); do
    P_NAME=$(echo "${PROVIDERS_JSON}" | jq -r ".[$i].name")
    P_ID=$(echo "${PROVIDERS_JSON}" | jq -r ".[$i].id")
    P_NEEDS_KEY=$(echo "${PROVIDERS_JSON}" | jq -r ".[$i].requires_api_key")

    if [[ "${P_NEEDS_KEY}" == "true" ]]; then
        KEY_NOTE="requires API key"
    else
        KEY_NOTE="no API key needed"
    fi

    printf "  ${BOLD}%d)${NC} %-22s ${YELLOW}(%s)${NC}\n" $((i + 1)) "${P_NAME}" "${KEY_NOTE}"
done

echo ""
while true; do
    read -rp "  Enter choice [1-${PROVIDER_COUNT}]: " PROVIDER_CHOICE
    if [[ "${PROVIDER_CHOICE}" =~ ^[0-9]+$ ]] && \
       [[ "${PROVIDER_CHOICE}" -ge 1 ]] && \
       [[ "${PROVIDER_CHOICE}" -le "${PROVIDER_COUNT}" ]]; then
        break
    fi
    echo -e "  ${RED}Invalid choice. Enter a number between 1 and ${PROVIDER_COUNT}.${NC}"
done

IDX=$((PROVIDER_CHOICE - 1))
SELECTED_PROVIDER_ID=$(echo "${PROVIDERS_JSON}" | jq -r ".[$IDX].id")
SELECTED_PROVIDER_NAME=$(echo "${PROVIDERS_JSON}" | jq -r ".[$IDX].name")
SELECTED_NEEDS_KEY=$(echo "${PROVIDERS_JSON}" | jq -r ".[$IDX].requires_api_key")

ok "Selected: ${SELECTED_PROVIDER_NAME} (${SELECTED_PROVIDER_ID})"

# =============================================================================
# Step 3: Enter API key (if required)
# =============================================================================
API_KEY=""

if [[ "${SELECTED_NEEDS_KEY}" == "true" ]]; then
    step "Enter API key for ${SELECTED_PROVIDER_NAME}"

    # Show provider-specific help
    case "${SELECTED_PROVIDER_ID}" in
        openai)       echo -e "  Get your key at: ${CYAN}https://platform.openai.com/api-keys${NC}" ;;
        anthropic)    echo -e "  Get your key at: ${CYAN}https://console.anthropic.com/settings/keys${NC}" ;;
        gemini)       echo -e "  Get your key at: ${CYAN}https://aistudio.google.com/apikey${NC}" ;;
        openrouter)   echo -e "  Get your key at: ${CYAN}https://openrouter.ai/keys${NC}" ;;
        vercel-ai-gateway) echo -e "  Get your key at: ${CYAN}https://vercel.com/dashboard${NC}" ;;
    esac

    echo ""
    while true; do
        read -rsp "  API key (hidden): " API_KEY
        echo ""
        if [[ -n "${API_KEY}" ]]; then
            break
        fi
        echo -e "  ${RED}API key cannot be empty.${NC}"
    done

    info "Storing API key..."
    curl -sf --max-time 10 -X POST "${ZENII_URL}/credentials" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg k "api_key:${SELECTED_PROVIDER_ID}" --arg v "${API_KEY}" '{key: $k, value: $v}')" > /dev/null

    ok "API key stored"
else
    step "No API key needed for ${SELECTED_PROVIDER_NAME}"
    ok "Skipping API key setup"
fi

# =============================================================================
# Step 4: Select model
# =============================================================================
step "Select a model for ${SELECTED_PROVIDER_NAME}"

MODELS_JSON=$(echo "${PROVIDERS_JSON}" | jq ".[$IDX].models")
MODEL_COUNT=$(echo "${MODELS_JSON}" | jq 'length')

if [[ "${MODEL_COUNT}" -eq 0 ]]; then
    warn "No built-in models for ${SELECTED_PROVIDER_NAME}"
    echo ""
    read -rp "  Enter a model ID manually: " SELECTED_MODEL_ID
    if [[ -z "${SELECTED_MODEL_ID}" ]]; then
        error "Model ID cannot be empty"
        exit 1
    fi
else
    echo ""
    for i in $(seq 0 $((MODEL_COUNT - 1))); do
        M_ID=$(echo "${MODELS_JSON}" | jq -r ".[$i].model_id")
        M_NAME=$(echo "${MODELS_JSON}" | jq -r ".[$i].display_name")
        M_CTX=$(echo "${MODELS_JSON}" | jq -r ".[$i].context_limit // empty")

        CTX_LABEL=""
        if [[ -n "${M_CTX}" ]] && [[ "${M_CTX}" != "null" ]]; then
            if [[ "${M_CTX}" -ge 1000000 ]]; then
                CTX_LABEL="$(( M_CTX / 1000 ))K ctx"
            elif [[ "${M_CTX}" -ge 1000 ]]; then
                CTX_LABEL="$(( M_CTX / 1000 ))K ctx"
            else
                CTX_LABEL="${M_CTX} ctx"
            fi
        fi

        printf "  ${BOLD}%d)${NC} %-35s ${YELLOW}%s${NC}\n" $((i + 1)) "${M_NAME} (${M_ID})" "${CTX_LABEL}"
    done

    CUSTOM_OPT=$((MODEL_COUNT + 1))
    printf "  ${BOLD}%d)${NC} %-35s\n" "${CUSTOM_OPT}" "Enter custom model ID"

    echo ""
    while true; do
        read -rp "  Enter choice [1-${CUSTOM_OPT}]: " MODEL_CHOICE
        if [[ "${MODEL_CHOICE}" =~ ^[0-9]+$ ]] && \
           [[ "${MODEL_CHOICE}" -ge 1 ]] && \
           [[ "${MODEL_CHOICE}" -le "${CUSTOM_OPT}" ]]; then
            break
        fi
        echo -e "  ${RED}Invalid choice. Enter a number between 1 and ${CUSTOM_OPT}.${NC}"
    done

    if [[ "${MODEL_CHOICE}" -eq "${CUSTOM_OPT}" ]]; then
        read -rp "  Enter model ID: " SELECTED_MODEL_ID
        if [[ -z "${SELECTED_MODEL_ID}" ]]; then
            error "Model ID cannot be empty"
            exit 1
        fi
    else
        MIDX=$((MODEL_CHOICE - 1))
        SELECTED_MODEL_ID=$(echo "${MODELS_JSON}" | jq -r ".[$MIDX].model_id")
    fi
fi

ok "Selected model: ${SELECTED_MODEL_ID}"

# =============================================================================
# Step 5: Confirm and set default
# =============================================================================
step "Confirm selection"

MASKED_KEY=""
if [[ -n "${API_KEY}" ]]; then
    if [[ ${#API_KEY} -gt 8 ]]; then
        MASKED_KEY="${API_KEY:0:8}***"
    else
        MASKED_KEY="***"
    fi
fi

echo ""
echo -e "  ${BOLD}Provider:${NC}  ${SELECTED_PROVIDER_NAME} (${SELECTED_PROVIDER_ID})"
echo -e "  ${BOLD}Model:${NC}     ${SELECTED_MODEL_ID}"
if [[ -n "${MASKED_KEY}" ]]; then
    echo -e "  ${BOLD}API Key:${NC}   ${MASKED_KEY}"
fi

echo ""
read -rp "  Proceed? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"

if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    info "Cancelled. Re-run the script to try again."
    exit 0
fi

info "Setting default provider..."
curl -sf --max-time 10 -X PUT "${ZENII_URL}/providers/default" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg p "${SELECTED_PROVIDER_ID}" --arg m "${SELECTED_MODEL_ID}" '{provider_id: $p, model_id: $m}')" > /dev/null

ok "Default provider: ${SELECTED_PROVIDER_ID} / ${SELECTED_MODEL_ID}"

# =============================================================================
# Step 6: Test connection
# =============================================================================
step "Testing connection to ${SELECTED_PROVIDER_NAME}"

TEST_RESULT=$(curl -sf --max-time 30 -X POST "${ZENII_URL}/providers/${SELECTED_PROVIDER_ID}/test" 2>/dev/null || echo "")

if [[ -n "${TEST_RESULT}" ]]; then
    TEST_OK=$(echo "${TEST_RESULT}" | jq -r '.success // false')
    TEST_MSG=$(echo "${TEST_RESULT}" | jq -r '.message // "No message"')
    TEST_MS=$(echo "${TEST_RESULT}" | jq -r '.latency_ms // "?"')

    if [[ "${TEST_OK}" == "true" ]]; then
        ok "Connection successful (${TEST_MS}ms)"
    else
        warn "Connection test failed: ${TEST_MSG}"
        warn "Chat may not work. Check your API key and try again."
    fi
else
    warn "Connection test timed out or failed"
fi

# =============================================================================
# Step 7: Test chat
# =============================================================================
step "Testing chat (asking Buddy who he is)"

echo ""
RESPONSE=$(curl -sf --max-time 120 -X POST "${ZENII_URL}/chat" \
    -H "Content-Type: application/json" \
    -d '{"prompt": "Hey Buddy, who are you? Keep it short."}' 2>/dev/null | jq -r '.response // empty' || echo "(no response)")

echo -e "  ${BOLD}Buddy says:${NC} ${RESPONSE}"
echo ""
if [[ "${RESPONSE}" != "(no response)" ]]; then
    ok "Chat is working"
else
    warn "Chat did not return a response — check daemon logs: journalctl -u zenii-pidog -f"
fi

# =============================================================================
# Step 8: Memory persistence demo
# =============================================================================
step "Testing memory persistence"

info "Storing a memory..."
curl -sf --max-time 10 -X POST "${ZENII_URL}/memory" \
    -H "Content-Type: application/json" \
    -d '{"key": "owner_info", "content": "My owner is Neil. He loves building robots and tinkering with Raspberry Pi."}' > /dev/null

ok "Memory stored"

info "Asking Buddy to recall..."
echo ""
RECALL=$(curl -sf --max-time 120 -X POST "${ZENII_URL}/chat" \
    -H "Content-Type: application/json" \
    -d '{"prompt": "What do you know about your owner?"}' 2>/dev/null | jq -r '.response // empty' || echo "(no response)")

echo -e "  ${BOLD}Buddy says:${NC} ${RECALL}"
echo ""
if [[ "${RECALL}" != "(no response)" ]]; then
    ok "Memory recall working"
else
    warn "Memory recall did not return a response"
fi

# =============================================================================
# Step 9: Personality swap demo
# =============================================================================
step "Personality swap demo"

if [[ -d "${PERSONAS_DIR}" ]]; then
    # --- Pirate mode ---
    info "Swapping to pirate personality..."
    PIRATE_MD=$(cat "${PERSONAS_DIR}/pirate.md")
    curl -sf -X PUT "${ZENII_URL}/identity/SOUL" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg c "$PIRATE_MD" '{content: $c}')" > /dev/null

    sleep 1
    echo ""
    PIRATE=$(curl -sf --max-time 60 -X POST "${ZENII_URL}/chat" \
        -H "Content-Type: application/json" \
        -d '{"prompt": "Ahoy! Introduce yourself, captain!"}' | jq -r '.response // .')

    echo -e "  ${BOLD}Pirate Buddy:${NC} ${PIRATE}"
    echo ""

    # --- Excited puppy mode ---
    info "Swapping to excited puppy personality..."
    PUPPY_MD=$(cat "${PERSONAS_DIR}/excited_puppy.md")
    curl -sf -X PUT "${ZENII_URL}/identity/SOUL" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg c "$PUPPY_MD" '{content: $c}')" > /dev/null

    sleep 1
    echo ""
    PUPPY=$(curl -sf --max-time 60 -X POST "${ZENII_URL}/chat" \
        -H "Content-Type: application/json" \
        -d '{"prompt": "Hey there! What are you excited about today?"}' | jq -r '.response // .')

    echo -e "  ${BOLD}Excited Buddy:${NC} ${PUPPY}"
    echo ""

    # --- Restore default ---
    info "Restoring default dog personality..."
    DEFAULT_MD=$(cat "${PERSONAS_DIR}/default_dog.md")
    curl -sf -X PUT "${ZENII_URL}/identity/SOUL" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg c "$DEFAULT_MD" '{content: $c}')" > /dev/null

    ok "Personalities work! Restored to default dog"
else
    warn "Personas directory not found at ${PERSONAS_DIR}"
    warn "Run pidog_setup_script.sh first"
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}============================================${NC}"
echo -e "${GREEN}${BOLD}  PiDog2 + Zenii is ready!${NC}"
echo -e "${GREEN}${BOLD}============================================${NC}"
echo ""
echo -e "  ${BOLD}Quick commands:${NC}"
echo ""
echo -e "  Provider:         ${SELECTED_PROVIDER_NAME} / ${SELECTED_MODEL_ID}"
echo -e "  Chat:             zenii chat 'Hey Buddy!'"
echo -e "  Health:           curl localhost:18981/health"
echo -e "  Store memory:     curl -X POST localhost:18981/memory -H 'Content-Type: application/json' -d '{\"key\": \"my_key\", \"content\": \"...\"}'"
echo -e "  View persona:     curl localhost:18981/identity/SOUL"
echo -e "  Daemon logs:      journalctl -u zenii-pidog -f"
echo -e "  Restart:          sudo systemctl restart zenii-pidog"
echo ""
echo -e "  ${CYAN}PiDog is the body. Zenii is the brain.${NC}"
echo ""
