# Zenii + PiDog2: Installation Guide

## Table of Contents

- [Why Zenii Matters for PiDog](#why-zenii-matters-for-pidog)
- [Architecture](#architecture)
- [Step 1: Download Pre-built ARM64 Binaries](#step-1-download-pre-built-arm64-binaries)
- [Step 2: Deploy & Configure on RPi4](#step-2-deploy--configure-on-rpi4)
- [Step 3: PiDog Persona (SOUL.md)](#step-3-pidog-persona-soulmd)
- [Step 4: Python Bridge](#step-4-python-bridge)
- [Step 5: Systemd Services](#step-5-systemd-services)
- [Key Files](#key-files)
- [Implementation Order](#implementation-order)

---

## Why Zenii Matters for PiDog

SunFounder sells PiDog ($180) with basic LLM wrappers (ChatGPT, Gemini, Ollama) via Python scripts. Their product page lists impressive hardware -- 12 servos, 32 actions, camera, sensors, STT/TTS -- but the AI is stateless:

### What PiDog Has Today (Without Zenii)

| Capability | PiDog Stock | Reality |
|---|---|---|
| LLM Integration | "ChatGPT-4o, Gemini, Ollama" | Simple Python wrapper, sends prompt, gets text back |
| Memory | None | Forgets everything between sessions and reboots |
| Personality | None | Generic LLM responses, no character |
| Tool Use | None | LLM can't search web, check system, or use tools |
| Provider Switching | Manual code changes | Hardcoded to one provider per script |
| Session History | None | Each interaction starts from zero |
| Credential Management | Hardcoded env vars | API keys in plaintext Python files |
| Sensor Awareness | Basic threshold triggers | Ultrasonic < X -> stop. No AI reasoning about sensor data |
| Programmability | Edit Python scripts | No API, no remote control, no extensibility |

### What PiDog Gets With Zenii

| Capability | With Zenii | How |
|---|---|---|
| **Persistent Memory** | Dog remembers your name, preferences, past conversations -- survives reboots | SQLite FTS5 + vector search, POST /memory |
| **Hot-Swap Personality** | Pirate dog, excited puppy, calm assistant -- switch in 2 seconds | SOUL.md + POST /identity/reload |
| **16 Agent Tools** | Web search, system info, file ops, memory store/recall | ToolRegistry, LLM calls tools autonomously |
| **Multi-Provider Routing** | Switch Claude/GPT/Ollama without touching code | PUT /providers/default |
| **Session Continuity** | Full conversation history, resume anytime | POST /sessions, GET /sessions/{id}/messages |
| **Secure Credentials** | OS keyring with encrypted file fallback | POST /credentials (keyring -> file -> memory) |
| **Sensor-Aware AI** | LLM reasons about distance, touch, sound direction, IMU | Sensor data injected as context in each prompt |
| **133 API Routes** | Full programmatic control -- build apps on top of the dog | HTTP/WS gateway on :18981 |
| **Learning** | Dog observes patterns, builds user profile over time | UserLearner + user_observations table |
| **Skills System** | Teach the dog new capabilities via markdown templates | POST /skills, SkillRegistry |

> **PiDog is the body. Zenii is the brain.**
>
> One 41MB binary. One `curl` command. The dog goes from toy to companion.

---

## Architecture

```
PiDog2 Hardware (Python)  <--HTTP/WS-->  Zenii Daemon (Rust, :18981)  <--API-->  LLM Provider
  servos, camera, sensors                  memory, personality, tools              Claude/GPT/Ollama
  speaker, LEDs, mic                       133 routes, SQLite
       ^                                         ^
       |                                         |
       +---- bridge/ (Python package, runs on RPi4) ----------+
```

**Key insight**: Zero Rust code changes needed. The entire PiDog integration is a Python bridge package that calls Zenii's existing HTTP/WS API. The daemon is unmodified.

---

## Step 1: Download Pre-built ARM64 Binaries

Pre-built ARM64 binaries are available on GitHub Releases (`app-v0.1.10`):

| Binary | Size | What |
|--------|------|------|
| `zenii-daemon-arm64` | **40.6 MB** | Headless daemon (all features) |
| `zenii-arm64` | **10.9 MB** | CLI client |

```bash
# On RPi4 — or just run 01_pidog_setup_script.sh which does all of this automatically
wget https://github.com/sprklai/zenii/releases/download/app-v0.1.10/zenii-daemon-arm64
wget https://github.com/sprklai/zenii/releases/download/app-v0.1.10/zenii-arm64
chmod +x zenii-daemon-arm64 zenii-arm64
sudo mv zenii-daemon-arm64 /usr/local/bin/zenii-daemon
sudo mv zenii-arm64 /usr/local/bin/zenii
```

No cross-compilation needed. Binaries include all features (channels, workflows, api-docs).

**Alternative: build from source** (if you want a leaner binary without channels/workflows):
```bash
# From x86 Linux dev machine
./scripts/build.sh --target linux-arm64 --release --crates "zenii-daemon zenii-cli"
scp dist/linux-arm64/release/zenii-daemon pi@pidog:~/
```

---

## Step 2: Deploy & Configure on RPi4

**Files created on RPi4 by `01_pidog_setup_script.sh`:**

```
/usr/local/bin/zenii-daemon          # Cross-compiled binary
/usr/local/bin/zenii                 # CLI binary

~/.config/zenii/config.toml          # Tuned for RPi4 + PiDog
~/.local/share/zenii/identity/
  SOUL.md                            # PiDog robot dog personality
  IDENTITY.md                        # Buddy metadata
~/.local/share/zenii/personas/
  default_dog.md
  pirate.md
  excited_puppy.md

/home/pi/pidog-zenii/
  .venv/                             # Python virtual environment
  bridge/                            # Bridge Python package
    __init__.py
    __main__.py
    config.py
    zenii_client.py
    hardware.py
    voice.py
    action_parser.py
    bridge.py
    requirements.txt

/etc/systemd/system/
  zenii-pidog.service                # Daemon auto-start
  pidog-bridge.service               # Bridge auto-start (depends on daemon)
```

**config.toml** (lean RPi4 config, cloud API):
```toml
gateway_host = "127.0.0.1"
gateway_port = 18981
log_level = "warn"
identity_name = "Buddy"
agent_max_tokens = 1024
agent_max_turns = 4
ws_max_connections = 4
memory_default_limit = 5
learning_enabled = true
```

Set API key on RPi4 after daemon starts:
```bash
curl -X POST localhost:18981/credentials \
  -H "Content-Type: application/json" \
  -d '{"key": "api_key:anthropic", "value": "sk-ant-..."}'

# Set default provider to Anthropic Claude
curl -X PUT localhost:18981/providers/default \
  -H "Content-Type: application/json" \
  -d '{"provider_id": "anthropic", "model_id": "claude-sonnet-4-6"}'
```

**Latency budget** (cloud API): mic -> STT (~1s local) -> HTTP to Zenii -> HTTP to Anthropic (~2-4s) -> parse + TTS (~1s) = **~4-6s total**. Fill wait time with "think" action + pulsing blue LEDs.

---

## Step 3: PiDog Persona (SOUL.md)

Replace default SOUL.md (crates/zenii-core/src/identity/defaults/SOUL.md) with a robot dog personality:

```markdown
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
```

---

## Step 4: Python Bridge

The bridge (`bridge/`) connects PiDog hardware to Zenii gateway. Three async loops share a `ThreadPoolExecutor` for blocking hardware I/O:

1. **Voice Loop**: mic -> STT (Vosk local or Pipecat cloud) -> prepend sensor context -> WS `/ws/chat` -> parse `<pidog_action>` tags -> TTS -> speaker + action queue
2. **Sensor Loop** (every 2s): read ultrasonic/touch/IMU/sound_direction -> POST `/memory` -> reactive triggers (touch = "being petted", obstacle <15cm = alert)
3. **Action Executor**: bounded queue (64 items), sequential servo execution to prevent conflicts

**Run the bridge:**
```bash
# Text mode (SSH/dev, no mic needed):
cd /home/pi/pidog-zenii
source .venv/bin/activate
PIDOG_VOICE_PROVIDER=text python3 -m bridge

# Local voice (Vosk STT + Piper TTS):
PIDOG_STT_MODEL=/home/pi/pidog-zenii/models/vosk-model-small-en-us-0.15 \
PIDOG_TTS_MODEL=/home/pi/pidog-zenii/models/piper/en_US-ryan-low.onnx \
    python3 -m bridge

# Simulation (dev machine, no PiDog hardware):
PIDOG_SIMULATE=true PIDOG_VOICE_PROVIDER=text python3 -m bridge
```

See `bridge/README.md` for full configuration options (voice providers, sensor tuning, systemd setup).

**WS protocol** (`sprklai/zenii` -> `src/gateway/handlers/ws.rs`):
- Send: `{"prompt": "...", "session_id": "...", "model": null}`
- Receive: `{"type": "text", "content": "..."}`, `{"type": "tool_call", ...}`, `{"type": "done"}`

**Sensor context injected before each prompt:**
```
[Sensors] Distance: 45cm | Touch: none | Sound: 270deg | IMU: stable | Time: 14:30
User said: "Hey Buddy, come here!"
```

**Action mapping table:**

| Zenii Action | PiDog API Call | Category |
|---|---|---|
| `forward` | `my_dog.do_action("forward", speed=N)` | Movement |
| `backward` | `my_dog.do_action("backward", speed=N)` | Movement |
| `turn_left` | `my_dog.do_action("turn_left", speed=N)` | Movement |
| `turn_right` | `my_dog.do_action("turn_right", speed=N)` | Movement |
| `sit` | `my_dog.do_action("sit")` | Posture |
| `stand` | `my_dog.do_action("stand")` | Posture |
| `lie_down` | `my_dog.do_action("lie_down")` | Posture |
| `bark` | `my_dog.do_action("bark")` | Expression |
| `howling` | `my_dog.do_action("howling")` | Expression |
| `wag_tail` | `my_dog.do_action("wag_tail")` | Expression |
| `shake_hand` | `my_dog.do_action("shake_hand")` | Trick |
| `high_five` | `my_dog.do_action("high_five")` | Trick |
| `push_up` | `my_dog.do_action("push_up")` | Trick |
| `nod` | `my_dog.do_action("nod")` | Expression |
| `think` | `my_dog.do_action("think")` | Expression |
| `surprise` | `my_dog.do_action("surprise")` | Expression |
| `leds_happy` | `rgb_strip.set_mode("breath", "#00FF00", 80)` | Mood |
| `leds_alert` | `rgb_strip.set_mode("blink", "#FF0000", 100)` | Mood |
| `leds_thinking` | `rgb_strip.set_mode("trail", "#0088FF", 50)` | Mood |
| `leds_idle` | `rgb_strip.set_mode("breath", "#333399", 20)` | Mood |

**Sensor-to-context mapping:**

| Sensor | Read Method | Context String | Reactive Trigger |
|---|---|---|---|
| Ultrasonic | `my_dog.ultrasonic.read_distance()` | `Distance: {N}cm` | `< 15cm` -> alert |
| Touch | `my_dog.dual_touch.read()` | `Touch: left/right/both/none` | Any touch -> "being petted" |
| Sound Dir | `my_dog.sound_direction.read_direction()` | `Sound: {N} degrees` | Sustained -> look toward |
| IMU | `my_dog.imu.read_accel()` | `IMU: {pitch}, {roll}` | Sudden change -> "picked up" |

---

## Step 5: Systemd Services

**zenii-pidog.service:**
```ini
[Unit]
Description=Zenii AI Daemon for PiDog
After=network.target

[Service]
Type=simple
User=pi
ExecStart=/usr/local/bin/zenii-daemon --config /home/pi/.config/zenii/config.toml
Restart=on-failure
RestartSec=5
Environment=RUST_LOG=warn

[Install]
WantedBy=multi-user.target
```

**pidog-bridge.service** (written by `01_pidog_setup_script.sh`):
```ini
[Unit]
Description=PiDog Zenii Bridge
After=zenii-pidog.service
Requires=zenii-pidog.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pidog-zenii
Environment=PIDOG_VOICE_PROVIDER=text
ExecStartPre=/bin/sleep 3
ExecStart=/home/pi/pidog-zenii/.venv/bin/python3 -m bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Key Files

### This repo (`sprklai/zenii-pidog`)

| File | Purpose |
|------|---------|
| `01_pidog_setup_script.sh` | One-command installer: binaries + config + identity + bridge + systemd |
| `02_post_install_script.sh` | Interactive provider/key setup + demo chat |
| `03_capabilities_test.sh` | Full capability test suite (12 categories) |
| `bridge/` | Python bridge package — hardware <-> Zenii gateway |
| `bridge/README.md` | Bridge setup, voice providers, env vars, troubleshooting |

### Zenii daemon (`sprklai/zenii` releases `app-v0.1.10`)

| Asset | Size | Purpose |
|-------|------|---------|
| `zenii-daemon-arm64` | ~44 MB | Headless daemon for RPi4 (aarch64) |
| `zenii-arm64` | ~11 MB | CLI client for RPi4 (aarch64) |

### Zenii source (`sprklai/zenii`) — reference only

| Path | Purpose |
|------|---------|
| `crates/zenii-core/src/gateway/handlers/ws.rs` | WS protocol (WsRequest/WsOutbound structs) |
| `crates/zenii-core/src/gateway/routes.rs` | All 133 API routes |
| `crates/zenii-core/src/identity/loader.rs` | SoulLoader + hot-reload |
| `crates/zenii-core/src/memory/sqlite_store.rs` | Persistent memory (FTS5 + vector) |

---

## Implementation Order

1. **Run `01_pidog_setup_script.sh` on RPi4** (10 min) -- installs binaries, config, identity, bridge, systemd
2. **Run `02_post_install_script.sh`** (5 min) -- set API key, provider, verify chat works
3. **Run `03_capabilities_test.sh`** (15 min) -- verify all 12 capability categories pass
4. **Configure voice** (1-2 hours) -- install Vosk model + Piper TTS or set Pipecat keys, restart bridge
5. **Test full interaction loop on physical dog** (2-3 hours) -- tune sensor thresholds, action speeds
