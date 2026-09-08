#!/usr/bin/env python3
"""
hotline.py — Luna Hotline: Voice-to-Voice Bridge

Connects an incoming phone call (Twilio) to the Luna LLM backend.

Pipeline per call:
    Twilio (mulaw 8 kHz) → Deepgram STT → Luna LLM → ElevenLabs TTS (ulaw_8000) → Twilio

Endpoints:
    POST /voice/incoming   — Twilio webhook, returns TwiML to open a Media Stream
    WS   /voice/stream     — Twilio Media Streams WebSocket (one per call)
    GET  /health           — health check

Run:
    uvicorn hotline:app --host 0.0.0.0 --port 8765

Required environment variables (add to .env):
    DEEPGRAM_API_KEY
    ELEVENLABS_API_KEY
    ELEVENLABS_VOICE_ID     Luna's ElevenLabs voice ID
    HOTLINE_BASE_URL        Public HTTPS base URL (e.g. https://abc.ngrok.io)
    # Shared with bot.py:
    OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_MODEL, VAULT_PATH, ...
"""

import asyncio
import base64
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

DEEPGRAM_API_KEY    = os.environ.get("DEEPGRAM_API_KEY", "")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL    = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
HOTLINE_BASE_URL    = os.environ.get("HOTLINE_BASE_URL", "").rstrip("/")

GREETING_MESSAGE = "Luna Hotline online. I'm here, Agent. How can I assist you?"
FALLBACK_MESSAGE = "The line is noisy, Agent. Please repeat."

# Re-chunk TTS output into 400 ms frames for smooth Twilio playback.
_AUDIO_CHUNK_BYTES = 3200  # 160 bytes × 20 ms = 3200 bytes ≈ 400 ms

app = FastAPI(title="Luna Hotline")
_executor = ThreadPoolExecutor(max_workers=8)


# ── TwiML Webhook ──────────────────────────────────────────────────────────────

@app.post("/voice/incoming")
async def voice_incoming(request: Request) -> Response:
    """Return TwiML telling Twilio to stream the call audio to our WebSocket."""
    ws_url = (
        HOTLINE_BASE_URL
        .replace("https://", "wss://")
        .replace("http://", "ws://")
    ) + "/voice/stream"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ── Per-Call Session ───────────────────────────────────────────────────────────

class CallSession:
    """All state for one voice call — never touches the Telegram bot's global context."""

    def __init__(self, stream_sid: str, call_sid: str = ""):
        self.stream_sid  = stream_sid
        self.call_sid    = call_sid
        self.session_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.started_at  = datetime.now()
        self.history: list[dict]         = []   # isolated LLM message history
        self.transcript_log: list[dict]  = []   # full turn log for vault
        self.pipeline_lock = asyncio.Lock()     # serialises LLM + TTS per call

    def add_turn(self, role: str, text: str) -> None:
        self.history.append({"role": role, "content": text})
        self.transcript_log.append({
            "role": role,
            "text": text,
            "ts": datetime.now().isoformat(),
        })


# ── Luna LLM — Voice-Isolated ─────────────────────────────────────────────────

def _voice_ask_luna(user_text: str, session: CallSession) -> str:
    """
    Run the Luna LLM for one utterance with call-isolated history.

    Calls llm._build_system_prompt() and llm._chat() directly so that
    voice sessions never pollute the Telegram bot's global context.deque.
    Mirrors the tool-call loop in llm.run().
    """
    import json as _json
    from llm import _build_system_prompt, _chat
    from tools import TOOL_HANDLERS

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = (
        f"{_build_system_prompt()}\n\n"
        f"Current date and time: {timestamp}. This is context only — do not act on it.\n\n"
        "## Voice Mode\n"
        "You are responding via a live phone call. Keep replies concise and natural "
        "for spoken audio — no markdown, no bullet points, no code blocks. "
        "Speak in complete sentences only. If a question requires a long answer, "
        "summarise and offer to continue."
    )

    session.add_turn("user", user_text)
    messages = [{"role": "system", "content": system_prompt}] + list(session.history)

    for _ in range(5):  # tool-call loop — mirrors llm.run()
        data    = _chat(messages)
        choice  = data["choices"][0]
        message = choice["message"]
        finish  = choice.get("finish_reason", "stop")

        if finish == "tool_calls" or message.get("tool_calls"):
            tool_calls = message["tool_calls"]
            messages.append({"role": "assistant", "tool_calls": tool_calls})
            session.history.append({"role": "assistant", "tool_calls": tool_calls})

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = _json.loads(tc["function"]["arguments"])
                except _json.JSONDecodeError:
                    fn_args = {}
                handler  = TOOL_HANDLERS.get(fn_name)
                result   = handler(fn_args) if handler else f"Error: unknown tool '{fn_name}'"
                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result}
                messages.append(tool_msg)
                session.history.append(tool_msg)
            continue

        reply = (message.get("content") or "").strip()
        session.add_turn("assistant", reply)
        return reply

    return FALLBACK_MESSAGE


# ── ElevenLabs TTS ─────────────────────────────────────────────────────────────

def _tts_to_ulaw(text: str) -> bytes | None:
    """
    Synthesise speech and return raw mulaw-8000 bytes via ElevenLabs.

    output_format=ulaw_8000 gives Twilio's native format directly —
    no audio conversion required.
    """
    if not (ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID and text):
        if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
            logger.warning("ElevenLabs not configured — TTS skipped")
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                url,
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": ELEVENLABS_MODEL,
                    "output_format": "ulaw_8000",
                    "voice_settings": {
                        "stability": 0.55,
                        "similarity_boost": 0.80,
                        "style": 0.25,
                        "use_speaker_boost": True,
                    },
                },
            )
            resp.raise_for_status()
            return resp.content
    except Exception:
        logger.exception("ElevenLabs TTS failed — text: %.80s", text)
        return None


# ── Deepgram Streaming STT ────────────────────────────────────────────────────

class DeepgramStreamer:
    """
    One Deepgram live-transcription WebSocket per call.

    Deepgram accepts mulaw-8000 bytes directly from Twilio (no format
    conversion) and fires on_final() for each completed utterance.
    """

    def __init__(self, on_final: callable):
        self._on_final   = on_final
        self._connection = None
        self._opened     = threading.Event()

    def start(self) -> None:
        from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

        dg   = DeepgramClient(DEEPGRAM_API_KEY)
        conn = dg.listen.websocket.v("1")

        conn.on(LiveTranscriptionEvents.Open,
                lambda *a, **kw: self._opened.set())
        conn.on(LiveTranscriptionEvents.Error,
                lambda self_c, err, **kw: logger.error("Deepgram error: %s", err))

        def _on_message(self_conn, result, **kwargs):
            try:
                transcript = result.channel.alternatives[0].transcript.strip()
                if transcript and result.is_final:
                    self._on_final(transcript)
            except Exception:
                logger.exception("Deepgram message handler error")

        conn.on(LiveTranscriptionEvents.Transcript, _on_message)

        conn.start(LiveOptions(
            model="nova-2",
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            endpointing=500,         # ms of silence → end of utterance
            interim_results=False,
            utterance_end_ms="1500",
        ))
        self._connection = conn
        self._opened.wait(timeout=10)

    def send(self, audio: bytes) -> None:
        if self._connection:
            try:
                self._connection.send(audio)
            except Exception:
                logger.debug("Deepgram send skipped — connection closing")

    def finish(self) -> None:
        if self._connection:
            try:
                self._connection.finish()
            except Exception:
                pass
            self._connection = None


# ── Vault Logging ─────────────────────────────────────────────────────────────

def _log_voice_session(session: CallSession) -> None:
    """Write the voice call transcript to the Sovereign Vault as a Voice Session note."""
    try:
        from tools import write_vault

        now      = session.started_at
        duration = int((datetime.now() - now).total_seconds())
        fname    = (
            f"Conversaties/Voice/"
            f"{now.strftime('%Y-%m-%d')}_{now.strftime('%H-%M')}"
            f"_Voice_{session.session_id}.md"
        )
        lines = [
            f"## Voice Sessie — {now.strftime('%Y-%m-%d %H:%M')}",
            "",
            (
                f"**Type:** Voice Session (Luna Hotline)  "
                f"|  **Duur:** {duration}s  "
                f"|  **Session:** `{session.session_id}`  "
                f"|  **Call:** `{session.call_sid}`"
            ),
            "",
            "---",
            "",
        ]
        for entry in session.transcript_log:
            lines.append(f"**{entry['role'].upper()}:** {entry['text']}")
            lines.append("")

        result = write_vault(fname, "\n".join(lines))
        logger.info("Voice session → vault: %s (%s)", fname, result)
    except Exception:
        logger.exception("Failed to log voice session to vault")


# ── WebSocket Handler ─────────────────────────────────────────────────────────

@app.websocket("/voice/stream")
async def voice_stream(ws: WebSocket) -> None:
    """
    One WebSocket per Twilio call.

    Twilio event sequence: connected → start → media* → stop

    - Forwards mulaw audio bytes to Deepgram.
    - On each final transcript: LLM → TTS → send audio.
    - On call end: log session to vault.
    """
    await ws.accept()

    session: CallSession | None         = None
    dg_streamer: DeepgramStreamer | None = None
    loop = asyncio.get_running_loop()

    async def _send_ulaw(ulaw_bytes: bytes) -> None:
        """Stream ulaw bytes to the caller in 400 ms chunks."""
        if not ulaw_bytes or session is None:
            return
        for i in range(0, len(ulaw_bytes), _AUDIO_CHUNK_BYTES):
            payload = base64.b64encode(ulaw_bytes[i : i + _AUDIO_CHUNK_BYTES]).decode()
            await ws.send_json({
                "event": "media",
                "streamSid": session.stream_sid,
                "media": {"payload": payload},
            })

    async def _speak(text: str, log_as_assistant: bool = True) -> None:
        """TTS a fixed string and send it — skips the LLM entirely."""
        if log_as_assistant and session:
            session.transcript_log.append({
                "role": "assistant",
                "text": text,
                "ts": datetime.now().isoformat(),
            })
        ulaw = await loop.run_in_executor(_executor, _tts_to_ulaw, text)
        if ulaw:
            await _send_ulaw(ulaw)

    async def _pipeline(transcript: str) -> None:
        """
        LLM → TTS → send for one utterance.
        Serialised by pipeline_lock so responses never overlap.
        Clears buffered audio first (interrupt handling).
        """
        async with session.pipeline_lock:
            logger.info("[%s] USER : %s", session.session_id, transcript)

            # Interrupt any audio currently playing
            await ws.send_json({"event": "clear", "streamSid": session.stream_sid})

            try:
                reply = await loop.run_in_executor(
                    _executor, _voice_ask_luna, transcript, session
                )
            except Exception:
                logger.exception("Luna LLM error")
                reply = FALLBACK_MESSAGE

            logger.info("[%s] LUNA : %s", session.session_id, reply)

            ulaw = await loop.run_in_executor(_executor, _tts_to_ulaw, reply)
            if ulaw:
                await _send_ulaw(ulaw)
            else:
                logger.warning("[%s] TTS returned no audio", session.session_id)

    def _on_transcript(transcript: str) -> None:
        """Deepgram callback (worker thread) — schedules the async pipeline."""
        asyncio.run_coroutine_threadsafe(_pipeline(transcript), loop)

    try:
        async for raw in ws.iter_text():
            msg   = json.loads(raw)
            event = msg.get("event")

            if event == "connected":
                logger.info("Twilio WS connected")

            elif event == "start":
                start_data = msg.get("start", {})
                session    = CallSession(
                    stream_sid=msg["streamSid"],
                    call_sid=start_data.get("callSid", ""),
                )
                logger.info(
                    "Call started — session=%s stream=%s call=%s",
                    session.session_id, session.stream_sid, session.call_sid,
                )

                # Start Deepgram (blocks until WebSocket open)
                dg_streamer = DeepgramStreamer(on_final=_on_transcript)
                await loop.run_in_executor(_executor, dg_streamer.start)

                # Greet the caller directly (no LLM needed).
                # Wrapped in pipeline_lock so any transcript arriving during
                # playback waits — prevents concurrent WebSocket writes.
                async def _greet() -> None:
                    async with session.pipeline_lock:
                        await _speak(GREETING_MESSAGE)

                asyncio.create_task(_greet())

            elif event == "media":
                if dg_streamer and session:
                    audio = base64.b64decode(msg["media"]["payload"])
                    loop.run_in_executor(_executor, dg_streamer.send, audio)

            elif event == "stop":
                logger.info("Call ended — session=%s", session.session_id if session else "?")
                break

    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected")
    except Exception:
        logger.exception("Unexpected error in voice_stream")
    finally:
        if dg_streamer:
            await loop.run_in_executor(_executor, dg_streamer.finish)
        if session:
            await loop.run_in_executor(_executor, _log_voice_session, session)
        logger.info("Call cleaned up — %s", session.session_id if session else "?")


# ── Health Check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "luna-hotline",
        "deepgram_configured": bool(DEEPGRAM_API_KEY),
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID),
        "hotline_url": HOTLINE_BASE_URL or "not set",
    }
