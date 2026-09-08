# Luna Hotline — Setup Guide

## Overview

The Hotline bridges a real phone number to the Luna LLM backend:

```
Caller → Twilio → hotline.py (FastAPI/WS) → Deepgram STT
                                           → Luna LLM
                                           → ElevenLabs TTS → Twilio → Caller
```

Voice sessions are logged automatically to the Sovereign Vault at
`Conversaties/Voice/YYYY-MM-DD_HH-MM_Voice_<session_id>.md`.

---

## 1. Prerequisites

```bash
# Install hotline dependencies
.venv/bin/pip install fastapi "uvicorn[standard]" deepgram-sdk websockets
```

---

## 2. Environment Variables

Add these to your `.env` file alongside the existing bot variables:

```ini
# ── Luna Hotline ──────────────────────────────────────────────────
DEEPGRAM_API_KEY=your_deepgram_api_key
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_VOICE_ID=your_elevenlabs_voice_id   # Luna's voice clone ID
HOTLINE_BASE_URL=https://your-public-url.ngrok.io

# Optional overrides
ELEVENLABS_MODEL=eleven_turbo_v2_5             # default; use eleven_multilingual_v2 for NL
```

### Getting API keys

| Service | URL |
|---------|-----|
| Deepgram | https://console.deepgram.com |
| ElevenLabs | https://elevenlabs.io → Profile → API Keys |
| ElevenLabs Voice ID | ElevenLabs → Voices → click Luna's voice → copy ID |

---

## 3. Twilio Configuration

### 3a. Buy a phone number

1. Log in to [Twilio Console](https://console.twilio.com).
2. Go to **Phone Numbers → Manage → Buy a number**.
3. Choose a number with **Voice** capability.

### 3b. Configure the webhook

1. Open your Twilio number → **Configure**.
2. Under **Voice Configuration → A call comes in**:
   - Set to **Webhook**
   - URL: `https://your-public-url/voice/incoming`
   - Method: `HTTP POST`
3. Save.

### 3c. Twilio Media Streams

Twilio Media Streams are enabled automatically via the TwiML `<Connect><Stream>` verb
returned by `/voice/incoming`. No additional console configuration is needed.

---

## 4. Public URL (development)

The WebSocket URL must be publicly reachable by Twilio. Use ngrok for local dev:

```bash
ngrok http 8765
# Copy the https URL → set HOTLINE_BASE_URL in .env
```

For production, deploy behind nginx/Caddy with a proper domain and TLS.

**nginx snippet** (after obtaining a cert via Certbot):

```nginx
server {
    listen 443 ssl;
    server_name hotline.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 300s;
    }
}
```

---

## 5. Running the Hotline

### Development

```bash
source .env  # or use direnv
.venv/bin/uvicorn hotline:app --host 0.0.0.0 --port 8765 --reload
```

### Production (systemd)

```bash
# Install the service
sudo cp hotline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hotline
sudo systemctl start hotline

# Check logs
sudo journalctl -u hotline -f
```

Both `sovereign-link.service` (Telegram bot) and `hotline.service` run independently
and share the same Luna backend and Vault.

---

## 6. Health Check

```bash
curl https://your-public-url/health
# {"status":"ok","service":"luna-hotline","deepgram_configured":true,...}
```

---

## 7. Voice Settings Tuning

ElevenLabs voice settings in `hotline.py` (`_tts_to_ulaw`):

| Parameter | Default | Effect |
|-----------|---------|--------|
| `stability` | 0.55 | Higher = more consistent, less expressive |
| `similarity_boost` | 0.80 | Higher = closer to the cloned voice |
| `style` | 0.25 | Higher = more style exaggeration |
| `use_speaker_boost` | true | Enhances clarity |

For Luna's warm/mysterious profile, `stability=0.50–0.60` and `style=0.20–0.35`
tend to work well.

---

## 8. Language

Deepgram is configured for `model=nova-2` (auto-detects language).
To force Dutch, change `LiveOptions(model="nova-2", language="nl", ...)` in
`DeepgramStreamer.start()`.

For Dutch TTS, set `ELEVENLABS_MODEL=eleven_multilingual_v2`.

---

## 9. Vault Integration

Voice sessions are written to:

```
<VAULT_PATH>/Conversaties/Voice/YYYY-MM-DD_HH-MM_Voice_<session_id>.md
```

Each note includes call duration, session ID, Twilio call SID, and the full
USER/LUNA transcript. The file is indexed by `write_vault()` immediately after
the call ends, so it is searchable by Luna in future sessions.
