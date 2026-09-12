#!/usr/bin/env python3
"""
voice_interface.py — Sovereign Link Web Voice Interface

Browser-based voice chat with Luna. Fully isolated from the Telegram bot's
conversation context — each browser session maintains its own history.

Pipeline per turn:
    Browser mic → WebM (Opus) → WebSocket → ffmpeg → 16kHz WAV
    → faster-whisper STT → LLM (tool loop) → edge-tts → MP3
    → WebSocket → browser playback

Run:
    uvicorn voice_interface:app --host 0.0.0.0 --port 8766

Then open http://localhost:8766 in any browser.

Required env vars (shared with bot.py):
    OLLAMA_BASE_URL, OLLAMA_MODEL, VAULT_PATH, WHISPER_MODEL, TTS_VOICE, ...
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import tts as _tts

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app      = FastAPI(title="Sovereign Link Voice Interface")
_pool    = ThreadPoolExecutor(max_workers=4)

_GREETING = "Sovereign Link actief. Ik luister, Agent."

_VOICE_WEB_ADDENDUM = (
    "\n\n## Voice Mode (Web Interface)\n"
    "Je reageert via een live webbrowser gesprek. Houd antwoorden beknopt en "
    "natuurlijk voor gesproken audio — geen markdown, geen opsommingstekens, "
    "geen codeblokken. Spreek in volledige zinnen. Bij complexe vragen: "
    "vat samen en bied aan om verder te gaan."
)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_HTML)


@app.websocket("/ws")
async def ws_handler(ws: WebSocket) -> None:
    await ws.accept()
    loop    = asyncio.get_running_loop()
    history: list[dict] = []
    logger.info("Voice interface: session opened")

    async def _send_json(payload: dict) -> None:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def _send_audio(path: str) -> None:
        try:
            with open(path, "rb") as f:
                await ws.send_bytes(f.read())
        except Exception:
            logger.exception("Failed to send audio to browser")
        finally:
            _tts._safe_remove(path)

    # ── Greet ──
    try:
        await _send_json({"type": "greeting", "text": _GREETING})
        greeting_path = await _tts.synthesize(_GREETING, prefer_mp3=True)
        if greeting_path:
            await _send_audio(greeting_path)
    except Exception:
        logger.exception("Greeting failed")

    await _send_json({"type": "status", "state": "listening"})

    # ── Main receive loop ──
    try:
        while True:
            data = await ws.receive()
            if data.get("type") == "websocket.disconnect":
                break

            audio_bytes: bytes | None = data.get("bytes")
            if not audio_bytes:
                continue

            tmp_webm = tempfile.mktemp(suffix=".webm")
            tmp_wav  = tempfile.mktemp(suffix=".wav")

            try:
                with open(tmp_webm, "wb") as f:
                    f.write(audio_bytes)

                # Convert browser WebM/Opus → 16kHz mono WAV for Whisper
                conv = await loop.run_in_executor(
                    _pool,
                    lambda: subprocess.run(
                        [
                            "ffmpeg", "-y", "-i", tmp_webm,
                            "-ar", "16000", "-ac", "1",
                            "-f", "wav", tmp_wav,
                        ],
                        capture_output=True,
                        timeout=30,
                    ),
                )
                if conv.returncode != 0:
                    logger.warning("ffmpeg conversion failed: %s", conv.stderr[:300].decode(errors="replace"))
                    await _send_json({"type": "error", "text": "Audio conversie mislukt."})
                    continue

                # STT
                from llm import transcribe_audio
                transcript: str = await loop.run_in_executor(_pool, transcribe_audio, tmp_wav)
                transcript = (transcript or "").strip()

                if not transcript:
                    await _send_json({"type": "status", "state": "listening"})
                    continue

                logger.info("Voice [USER]: %s", transcript)
                await _send_json({"type": "transcript", "text": transcript})
                await _send_json({"type": "status",     "state": "thinking"})

                # LLM (isolated from Telegram's global context)
                reply: str = await loop.run_in_executor(
                    _pool, _voice_ask_luna, transcript, history
                )
                logger.info("Voice [LUNA]: %s", reply[:120])

                await _send_json({"type": "response_text", "text": reply})
                await _send_json({"type": "status",        "state": "speaking"})

                # TTS → MP3 (browser plays natively)
                audio_path = await _tts.synthesize(reply, prefer_mp3=True)
                if audio_path:
                    await _send_audio(audio_path)

                await _send_json({"type": "status", "state": "listening"})

            except asyncio.TimeoutError:
                await _send_json({"type": "error", "text": "Verzoek duurde te lang."})
            finally:
                _tts._safe_remove(tmp_webm)
                _tts._safe_remove(tmp_wav)

    except WebSocketDisconnect:
        logger.info("Voice interface: session closed")
    except Exception:
        logger.exception("Voice interface: unexpected error")


# ── Isolated LLM call ──────────────────────────────────────────────────────────

def _voice_ask_luna(user_text: str, history: list[dict]) -> str:
    """
    One LLM turn with call-isolated history.

    Mirrors hotline._voice_ask_luna() but targets the web interface.
    Never reads from or writes to the Telegram bot's global context.deque.
    Runs the full tool-call loop so Luna retains all tool access.
    """
    import json as _json
    from llm import _build_system_prompt, _chat
    from tools import TOOL_HANDLERS

    timestamp     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = (
        f"{_build_system_prompt()}\n\n"
        f"Current date and time: {timestamp}. This is context only — do not act on it."
        + _VOICE_WEB_ADDENDUM
    )

    history.append({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": system_prompt}] + history

    for _ in range(10):
        data    = _chat(messages)
        choice  = data["choices"][0]
        message = choice["message"]
        finish  = choice.get("finish_reason", "stop")

        if finish == "tool_calls" or message.get("tool_calls"):
            tool_calls = message["tool_calls"]
            messages.append({"role": "assistant", "tool_calls": tool_calls})
            history.append({"role": "assistant", "tool_calls": tool_calls})

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = _json.loads(tc["function"]["arguments"])
                except _json.JSONDecodeError:
                    fn_args = {}
                handler = TOOL_HANDLERS.get(fn_name)
                result  = handler(fn_args) if handler else f"Error: unknown tool '{fn_name}'"
                if isinstance(result, str) and len(result) > 6000:
                    result = result[:6000] + "\n[…output truncated]"
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                }
                messages.append(tool_msg)
                history.append(tool_msg)
            continue

        reply = (message.get("content") or "").strip()
        history.append({"role": "assistant", "content": reply})
        return reply

    return "Ik kon geen antwoord formuleren. Probeer het opnieuw."


# ── Embedded HTML/JS interface ─────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sovereign Link — Voice</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:           #0d0d0d;
      --surface:      #161616;
      --accent:       #7c5cbf;
      --accent-dim:   #4a3571;
      --accent-light: #9b7fd4;
      --text:         #e2e2e2;
      --text-dim:     #666;
      --user-bg:      #162216;
      --user-border:  #223322;
      --luna-bg:      #14111f;
      --luna-border:  #271d40;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      height: 100dvh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    /* ── Header ── */
    header {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      padding: 0.8rem 1.2rem;
      border-bottom: 1px solid #1c1c1c;
      flex-shrink: 0;
    }
    .dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      background: #444;
      transition: background 0.4s;
      flex-shrink: 0;
    }
    header h1 {
      font-size: 0.78rem;
      font-weight: 500;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--text-dim);
    }

    /* ── Chat ── */
    #chat {
      flex: 1;
      overflow-y: auto;
      padding: 1.2rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    #chat::-webkit-scrollbar { width: 3px; }
    #chat::-webkit-scrollbar-track { background: transparent; }
    #chat::-webkit-scrollbar-thumb { background: #252525; border-radius: 2px; }

    .bubble {
      max-width: 78%;
      padding: 0.6rem 0.85rem;
      border-radius: 0.9rem;
      font-size: 0.92rem;
      line-height: 1.55;
    }
    .bubble.user {
      background: var(--user-bg);
      border: 1px solid var(--user-border);
      align-self: flex-end;
      border-bottom-right-radius: 0.2rem;
    }
    .bubble.luna {
      background: var(--luna-bg);
      border: 1px solid var(--luna-border);
      align-self: flex-start;
      border-bottom-left-radius: 0.2rem;
    }
    .label {
      font-size: 0.65rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--text-dim);
      margin-bottom: 0.2rem;
    }
    .bubble.luna .label { color: var(--accent-light); }

    /* ── Footer ── */
    footer {
      flex-shrink: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.55rem;
      padding: 1rem 1.2rem 1.4rem;
      border-top: 1px solid #1c1c1c;
    }

    #status {
      font-size: 0.76rem;
      color: var(--text-dim);
      min-height: 1.1em;
      text-align: center;
    }

    #mic-btn {
      position: relative;
      width: 66px; height: 66px;
      border-radius: 50%;
      border: none;
      background: var(--accent);
      color: #fff;
      font-size: 1.65rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      user-select: none;
      -webkit-user-select: none;
      outline: none;
      transition: background 0.15s;
    }
    #mic-btn:hover:not(.recording) { background: var(--accent-light); }
    #mic-btn:active:not(.recording) { transform: scale(0.96); }

    #mic-btn.recording {
      background: #c0392b;
      animation: rec-pulse 1.1s ease-in-out infinite;
    }
    @keyframes rec-pulse {
      0%, 100% { box-shadow: 0 0 0 0   rgba(192,57,43,0.4); }
      50%       { box-shadow: 0 0 0 14px rgba(192,57,43,0);   }
    }

    #hint {
      font-size: 0.68rem;
      color: #393939;
    }

    /* Waveform bars shown while speaking */
    #waveform {
      display: none;
      gap: 3px;
      align-items: flex-end;
      height: 18px;
    }
    #waveform.active { display: flex; }
    #waveform span {
      width: 3px;
      border-radius: 2px;
      background: var(--accent-light);
      animation: bar 0.7s ease-in-out infinite;
    }
    #waveform span:nth-child(2) { animation-delay: .1s; }
    #waveform span:nth-child(3) { animation-delay: .2s; }
    #waveform span:nth-child(4) { animation-delay: .3s; }
    #waveform span:nth-child(5) { animation-delay: .2s; }
    @keyframes bar {
      0%,100% { height: 4px; }
      50%      { height: 16px; }
    }
  </style>
</head>
<body>

<header>
  <div class="dot" id="conn-dot"></div>
  <h1>Sovereign Link &mdash; Voice</h1>
</header>

<div id="chat" role="log" aria-live="polite" aria-label="Gesprek"></div>

<footer>
  <div id="status">Verbinden&hellip;</div>
  <button id="mic-btn" aria-label="Microfoon — druk om te spreken">&#127897;</button>
  <div id="waveform" aria-hidden="true">
    <span></span><span></span><span></span><span></span><span></span>
  </div>
  <div id="hint">Klik &amp; vasthouden &bull; of houd Spatie vast</div>
</footer>

<script>
(() => {
  'use strict';

  const chat     = document.getElementById('chat');
  const btn      = document.getElementById('mic-btn');
  const statusEl = document.getElementById('status');
  const connDot  = document.getElementById('conn-dot');
  const waveform = document.getElementById('waveform');

  const WS_URL   = `ws://${location.host}/ws`;

  let ws, mediaRecorder;
  let recording   = false;
  const chunks    = [];

  // Sequential audio playback — never overlaps Luna's replies
  let audioQueue  = Promise.resolve();

  // ── WebSocket ─────────────────────────────────────────
  function connect() {
    ws             = new WebSocket(WS_URL);
    ws.binaryType  = 'arraybuffer';

    ws.onopen  = () => {
      connDot.style.background = '#2ecc71';
      setStatus('Verbonden &mdash; klik of houd spatie om te spreken');
    };
    ws.onclose = () => {
      connDot.style.background = '#e74c3c';
      setStatus('Verbinding verbroken. Herladen&hellip;');
      setWave(false);
      setTimeout(connect, 3000);
    };
    ws.onerror = () => { connDot.style.background = '#e74c3c'; };

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        enqueueAudio(e.data);
      } else {
        handleMessage(JSON.parse(e.data));
      }
    };
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case 'greeting':      addBubble('luna', msg.text); break;
      case 'transcript':    addBubble('user', msg.text); break;
      case 'response_text': addBubble('luna', msg.text); break;
      case 'status':        applyStatus(msg.state);      break;
      case 'error':         setStatus('&#9888; ' + msg.text); break;
    }
  }

  function applyStatus(state) {
    const labels = {
      listening: 'Luisteren&hellip;',
      thinking:  'Denken&hellip;',
      speaking:  'Luna spreekt&hellip;',
    };
    setStatus(labels[state] || '');
    setWave(state === 'speaking');
  }

  // ── Audio playback queue ──────────────────────────────
  function enqueueAudio(buffer) {
    audioQueue = audioQueue.then(() => new Promise((resolve) => {
      const blob = new Blob([buffer], { type: 'audio/mpeg' });
      const url  = URL.createObjectURL(blob);
      const a    = new Audio(url);
      a.onended  = () => { URL.revokeObjectURL(url); resolve(); };
      a.onerror  = () => { URL.revokeObjectURL(url); resolve(); };
      a.play().catch(resolve);
    }));
  }

  // ── MediaRecorder ─────────────────────────────────────
  navigator.mediaDevices.getUserMedia({ audio: true, video: false })
    .then((stream) => {
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm';
      mediaRecorder = new MediaRecorder(stream, { mimeType });

      mediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data);
      };

      mediaRecorder.onstop = () => {
        if (!chunks.length) return;
        const blob = new Blob(chunks, { type: mediaRecorder.mimeType });
        chunks.length = 0;
        if (ws?.readyState === WebSocket.OPEN) {
          blob.arrayBuffer().then((buf) => ws.send(buf));
        }
        setStatus('Verwerken&hellip;');
      };
    })
    .catch((err) => {
      setStatus('Microfoon geweigerd: ' + err.message);
      btn.disabled = true;
    });

  function startRec() {
    if (!mediaRecorder || recording || mediaRecorder.state === 'recording') return;
    chunks.length = 0;
    try { mediaRecorder.start(100); } catch { return; }
    recording = true;
    btn.classList.add('recording');
    btn.innerHTML = '&#9209;';  // ⏹
    setStatus('Opnemen&hellip; laat los om te versturen');
    setWave(false);
  }

  function stopRec() {
    if (!recording) return;
    if (mediaRecorder?.state === 'recording') {
      try { mediaRecorder.stop(); } catch {}
    }
    recording = false;
    btn.classList.remove('recording');
    btn.innerHTML = '&#127897;'; // 🎙
  }

  // ── Input bindings ────────────────────────────────────
  btn.addEventListener('mousedown',   (e) => { e.preventDefault(); startRec(); });
  btn.addEventListener('mouseup',     (e) => { e.preventDefault(); stopRec();  });
  btn.addEventListener('mouseleave',  stopRec);
  btn.addEventListener('touchstart',  (e) => { e.preventDefault(); startRec(); }, { passive: false });
  btn.addEventListener('touchend',    (e) => { e.preventDefault(); stopRec();  }, { passive: false });
  btn.addEventListener('touchcancel', stopRec);

  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && !e.repeat && document.activeElement === document.body) {
      e.preventDefault();
      startRec();
    }
  });
  document.addEventListener('keyup', (e) => {
    if (e.code === 'Space') stopRec();
  });

  // ── UI helpers ────────────────────────────────────────
  function addBubble(role, text) {
    const wrap  = document.createElement('div');
    wrap.className = 'bubble ' + role;
    const label = document.createElement('div');
    label.className = 'label';
    label.textContent = role === 'user' ? 'Jij' : 'Luna';
    const body = document.createElement('div');
    body.textContent = text;
    wrap.append(label, body);
    chat.appendChild(wrap);
    requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
  }

  function setStatus(html)   { statusEl.innerHTML = html; }
  function setWave(active)   { waveform.classList.toggle('active', active); }

  connect();
})();
</script>
</body>
</html>
"""
