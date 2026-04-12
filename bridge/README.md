# PiDog2-Zenii Bridge: Migration & Deployment Guide

Python bridge connecting PiDog2 robot hardware to the Zenii AI daemon.
Three async loops — voice, sensors, actions — share a thread pool for
blocking hardware I/O while the event loop handles WS/HTTP to Zenii.

```
PiDog2 Hardware ←→ Bridge (Python, this code) ←→ Zenii Daemon (:18981) ←→ LLM
  servos, LEDs        voice, sensors, actions       memory, tools, identity     Claude/GPT/Ollama
  mic, speaker        3 async loops + thread pool   114 API routes
```

---

## Prerequisites (on RPi4)

```bash
# System packages
sudo apt update && sudo apt install -y \
    python3 python3-pip python3-venv \
    portaudio19-dev libffi-dev libssl-dev \
    alsa-utils

# Verify Python 3.9+
python3 --version
```

---

## Step 1: Copy Bridge Files to PiDog

### Option A: SCP from dev machine

```bash
# From your development machine (from zenii-pidog repo root)
scp -r bridge/ pi@<PIDOG_IP>:/home/pi/pidog-zenii/bridge/
```

### Option B: Git clone (if repo is accessible)

```bash
# On the RPi4
git clone https://github.com/sprklai/zenii-pidog.git /tmp/zenii-pidog
cp -r /tmp/zenii-pidog/bridge/ /home/pi/pidog-zenii/bridge/
rm -rf /tmp/zenii-pidog
```

### Option C: Manual copy

Create the directory structure on RPi4:

```
/home/pi/pidog-zenii/
└── bridge/
    ├── __init__.py
    ├── __main__.py
    ├── config.py
    ├── zenii_client.py
    ├── hardware.py
    ├── voice.py
    ├── action_parser.py
    ├── bridge.py
    └── requirements.txt
```

Copy each file from `go2market/pidog/bridge/` to `/home/pi/pidog-zenii/bridge/`.

---

## Step 2: Install Dependencies

```bash
cd /home/pi/pidog-zenii

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install core dependencies
pip install -r bridge/requirements.txt
```

### Local Voice (Vosk STT + Piper TTS)

```bash
# Download Vosk model (small, fast for RPi4)
mkdir -p models
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip -d models/
rm vosk-model-small-en-us-0.15.zip

# Install Piper TTS binary
wget https://github.com/rhasspy/piper/releases/latest/download/piper_linux_aarch64.tar.gz
tar -xzf piper_linux_aarch64.tar.gz
sudo mv piper/piper /usr/local/bin/
rm -rf piper piper_linux_aarch64.tar.gz

# Download Piper voice model
mkdir -p models/piper
wget -P models/piper/ \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/low/en_US-ryan-low.onnx
wget -P models/piper/ \
    https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/low/en_US-ryan-low.onnx.json
```

### Cloud Voice (optional)

No extra packages needed — providers are called directly via REST using the bundled
`aiohttp` library. Just set your API keys as environment variables or in the TOML config.

---

## Step 3: Configure

Configuration is via environment variables (highest priority), optional TOML
file, or defaults. All settings have sensible defaults for RPi4.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| **Connection** | | |
| `ZENII_URL` | `http://127.0.0.1:18981` | Zenii daemon URL |
| `ZENII_TOKEN` | *(none)* | Bearer token (if auth configured) |
| **Voice** | | |
| `PIDOG_VOICE_PROVIDER` | `local` | `local`, `pipecat`, or `text` |
| `PIDOG_STT_MODEL` | *(none)* | Path to Vosk model directory (local mode) |
| `PIDOG_TTS_MODEL` | `en_US-ryan-low` | Piper TTS model name (local mode) |
| `PIDOG_TTS_BINARY` | `piper` | Path to piper binary (local mode) |
| **Pipecat** | | |
| `PIPECAT_STT_PROVIDER` | `deepgram` | STT provider: `deepgram`, `google`, `azure` |
| `PIPECAT_TTS_PROVIDER` | `cartesia` | TTS provider: `cartesia`, `elevenlabs`, `azure`, `google` |
| `PIPECAT_STT_API_KEY` | *(none)* | API key for STT provider |
| `PIPECAT_TTS_API_KEY` | *(none)* | API key for TTS provider |
| `PIPECAT_TTS_VOICE` | *(none)* | Voice ID (provider-specific) |
| `PIPECAT_STT_MODEL` | *(none)* | STT model (provider-specific) |
| `PIPECAT_TTS_MODEL` | *(none)* | TTS model (provider-specific) |
| **Hardware** | | |
| `PIDOG_SIMULATE` | `false` | `true` for development without PiDog |
| `PIDOG_DEFAULT_SPEED` | `80` | Default servo speed (0-100) |
| **Sensors** | | |
| `PIDOG_SENSOR_INTERVAL` | `2.0` | Sensor read interval (seconds) |
| `PIDOG_MEMORY_THROTTLE` | `30.0` | Min interval between sensor memory POSTs |
| `PIDOG_OBSTACLE_ALERT_CM` | `15` | Distance threshold for obstacle alert |
| `PIDOG_TOUCH_COOLDOWN` | `60.0` | Cooldown between "being petted" events |
| **Concurrency** | | |
| `PIDOG_THREAD_POOL_SIZE` | `6` | Thread pool size for blocking I/O |
| `PIDOG_WS_CHAT_TIMEOUT` | `120.0` | Max wait for AI response (seconds) |
| `PIDOG_ACTION_TIMEOUT` | `10.0` | Max wait per servo action (seconds) |
| `PIDOG_SENSOR_READ_TIMEOUT` | `5.0` | Max wait for sensor read (seconds) |
| **Reconnection** | | |
| `PIDOG_WS_RECONNECT_DELAY` | `2.0` | Initial WS reconnect delay (seconds) |
| `PIDOG_WS_MAX_RECONNECT_DELAY` | `60.0` | Max WS reconnect delay (seconds) |

### Optional TOML Config

Create `/home/pi/pidog-zenii/bridge_config.toml`:

```toml
zenii_url = "http://127.0.0.1:18981"
simulate_hardware = false
thread_pool_size = 6

[voice]
provider = "pipecat"

[voice.pipecat]
stt_provider = "deepgram"
tts_provider = "cartesia"
stt_api_key = "your-deepgram-key"
tts_api_key = "your-cartesia-key"
tts_voice = "your-voice-id"
```

Then set: `export PIDOG_CONFIG=/home/pi/pidog-zenii/bridge_config.toml`

---

## Step 4: Run

### Simulation Mode (no hardware — laptop/dev)

```bash
cd /home/pi/pidog-zenii
source .venv/bin/activate

# Text mode, simulated hardware
PIDOG_SIMULATE=true PIDOG_VOICE_PROVIDER=text \
    python3 -m bridge
```

### Local Voice (RPi4 with mic/speaker)

```bash
PIDOG_STT_MODEL=/home/pi/pidog-zenii/models/vosk-model-small-en-us-0.15 \
PIDOG_TTS_MODEL=/home/pi/pidog-zenii/models/piper/en_US-ryan-low.onnx \
    python3 -m bridge
```

### Pipecat Cloud Voice

```bash
PIDOG_VOICE_PROVIDER=pipecat \
PIPECAT_STT_PROVIDER=deepgram \
PIPECAT_STT_API_KEY=your-key \
PIPECAT_TTS_PROVIDER=cartesia \
PIPECAT_TTS_API_KEY=your-key \
PIPECAT_TTS_VOICE=your-voice-id \
    python3 -m bridge
```

### Stop

`Ctrl+C` or `kill -SIGTERM <pid>` — the bridge performs graceful shutdown
(stand up, LEDs off, close WS).

---

## Step 5: Systemd Service (auto-start)

Create `/etc/systemd/system/pidog-bridge.service`:

```ini
[Unit]
Description=PiDog Zenii Bridge
After=zenii-pidog.service
Requires=zenii-pidog.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pidog-zenii
Environment=PIDOG_STT_MODEL=/home/pi/pidog-zenii/models/vosk-model-small-en-us-0.15
Environment=PIDOG_TTS_MODEL=/home/pi/pidog-zenii/models/piper/en_US-ryan-low.onnx
ExecStartPre=/bin/sleep 3
ExecStart=/home/pi/pidog-zenii/.venv/bin/python3 -m bridge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable pidog-bridge
sudo systemctl start pidog-bridge
sudo systemctl status pidog-bridge
sudo journalctl -u pidog-bridge -f    # View logs
```

---

## Architecture

### Three Async Loops

| Loop | Frequency | Thread Pool | Purpose |
|------|-----------|------------|---------|
| **Voice** | On speech detected | Audio recording, TTS playback | Listen -> WS chat -> speak + enqueue actions |
| **Sensor** | Every 2s | Hardware sensor reads | Read sensors -> reactive triggers -> memory store |
| **Action** | On queue item | Servo/LED commands | Sequential execution (prevents servo conflicts) |

### Concurrency Model

```
asyncio event loop (single thread)
├── Voice Loop ────── listen() ──────> ThreadPool (mic recording)
│                     ws_chat() ─────> async WS I/O (no thread)
│                     speak() ───────> ThreadPool (TTS + playback)
├── Sensor Loop ───── read_sensors() > ThreadPool (I2C/SPI reads)
│                     store_memory() > async HTTP (fire-and-forget)
├── Action Executor── execute_action() > ThreadPool (servo I2C)
│                     set_leds() ────> ThreadPool (RGB strip)
└── Shutdown Watcher
```

All blocking I/O runs in the `ThreadPoolExecutor` (default 6 threads).
The event loop never blocks — WS/HTTP are native async via aiohttp.

### Timeout Protection

Every async operation has a timeout to prevent hanging:

| Operation | Timeout | On Timeout |
|-----------|---------|------------|
| WS chat response | 120s | Log warning, return to idle |
| Sensor read | 5s | Log warning, use last reading |
| Servo action | 10s | Log warning, skip action |
| TTS synthesis | 30s | Log warning, skip speech |
| Health check | 5s | Retry with backoff |
| Memory store | 5s | Log debug, continue |

### Action Queue

- Bounded queue (64 items) prevents memory growth
- Non-blocking enqueue (`put_nowait`) — drops if full
- Sequential dequeue ensures servo commands don't conflict
- LEDs and servos share the same queue for ordered execution

---

## Voice Provider Comparison

| Provider | Latency | Offline | Cost | Best For |
|----------|---------|---------|------|----------|
| `local` (Vosk+Piper) | ~1s STT + ~1s TTS | Yes | Free | RPi4 demos, no internet |
| `pipecat` (Deepgram+Cartesia) | ~300ms STT + ~200ms TTS | No | Pay-per-use | Low-latency, natural voice |
| `pipecat` (Deepgram+ElevenLabs) | ~300ms STT + ~500ms TTS | No | Pay-per-use | Voice cloning |
| `text` (stdin/stdout) | Instant | Yes | Free | Development, SSH |

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| "Daemon not ready" loop | Zenii daemon not running | `sudo systemctl start zenii-pidog` |
| "WS connect failed" | Auth token mismatch | Check `ZENII_TOKEN` matches daemon config |
| "Microphone read failed" | No mic or wrong device | `arecord -l` to list devices, check ALSA config |
| "TTS binary not found" | Piper not installed | See Step 2 (Install Piper) |
| "pidog library not found" | SunFounder lib missing | `pip install pidog` or run on actual PiDog |
| "aiohttp not installed" | Missing core dep | `pip install -r bridge/requirements.txt` |
| "Action queue full" | Actions enqueued faster than executed | Increase `PIDOG_ACTION_TIMEOUT`, check servo response |
| "Sensor read timed out" | I2C bus contention | Increase `PIDOG_SENSOR_READ_TIMEOUT` to 10s |
| Bridge hangs on shutdown | Stuck servo command | Force kill: `kill -9`, then investigate hardware |

### Logs

```bash
# Increase log verbosity
PYTHONUNBUFFERED=1 python3 -m bridge 2>&1 | tee bridge.log

# Debug level (shows action/LED details, WS messages)
# Edit __main__.py: level=logging.DEBUG
```

---

## Development Workflow

Run the full bridge on your laptop without any PiDog hardware:

```bash
# Terminal 1: Start Zenii daemon
cargo run -p zenii-daemon

# Terminal 2: Run bridge in simulation + text mode (from zenii-pidog repo root)
cd /path/to/zenii-pidog
PIDOG_SIMULATE=true PIDOG_VOICE_PROVIDER=text python3 -m bridge
```

Type messages at the `You>` prompt. The bridge will:
1. Send your text to Zenii via WebSocket
2. Parse `<pidog_action>` tags from the AI response
3. Log simulated actions (e.g., `[SIM] Action: wag_tail speed=80`)
4. Print the clean response text as `Buddy> ...`
5. Log sensor readings and memory stores

---

## File Reference

| File | LOC | Purpose |
|------|-----|---------|
| `__init__.py` | 3 | Package marker + version |
| `__main__.py` | 50 | Entry point, thread pool, signal handlers |
| `config.py` | 200 | BridgeConfig dataclass, env var + TOML loading |
| `zenii_client.py` | 210 | Async HTTP + WS client for Zenii API |
| `hardware.py` | 200 | PiDog hardware abstraction (real + simulated) |
| `voice.py` | 320 | Voice providers (Local, Pipecat, Text) |
| `action_parser.py` | 120 | Parse `<pidog_action>` / `<pidog_leds>` tags |
| `bridge.py` | 310 | Main bridge: 3 async loops + orchestration |
| `requirements.txt` | 15 | Python dependencies |
