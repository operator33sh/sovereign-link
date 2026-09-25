"""
api_server.py — OpenAI-compatible HTTP API voor Open WebUI

Exposeert:
  POST /v1/chat/completions  — stuurt via llm.run() incl. vault logging + tools
  GET  /v1/models            — modellijst voor Open WebUI discovery

Poort: 11435 (Ollama gebruikt 11434)

Starten:
  .venv/bin/python api_server.py
"""

import asyncio
import json
import logging
import os
import time
import uuid

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import cognitive_brake
import context
import llm
from session_logger import session_logger

app = FastAPI(title="Sovereign Link API", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    cognitive_brake.ensure_monitor_running()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = ""
    messages: list[Message]
    stream: bool = False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/models")
def list_models():
    model_name = os.environ.get("OLLAMA_MODEL", "sovereign-link")
    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "sovereign-link",
            }
        ],
    }


async def _sse_stream(reply: str, model_name: str):
    """Async generator: yields OpenAI-compatible SSE chunks with perceptual pacing."""
    import fluidity

    cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    for chunk, delay in fluidity.iter_stream_chunks(reply):
        if delay > 0:
            await asyncio.sleep(delay)
        data = json.dumps({
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
        })
        yield f"data: {data}\n\n"

    # Final chunk — signals stream end
    final = json.dumps({
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    yield f"data: {final}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    # Sync server context with Open WebUI's conversation history.
    # Open WebUI is the source of truth — reset and rebuild on every request
    # so stale server-side context never causes the model to go off-topic.
    non_system = [m for m in req.messages if m.role != "system"]
    if not non_system:
        raise HTTPException(status_code=400, detail="Geen user message gevonden")

    last_msg = non_system[-1]
    if last_msg.role != "user":
        raise HTTPException(status_code=400, detail="Laatste bericht is geen user message")

    user_text = last_msg.content
    prior_messages = non_system[:-1]  # history before the current turn

    # Reset and restore from Open WebUI history
    context.clear()
    for msg in prior_messages:
        context.add_message(msg.role, msg.content)

    loop = asyncio.get_event_loop()
    try:
        reply = await loop.run_in_executor(None, lambda: llm.run(user_text))
        session_logger.on_turn(context.get_history())
    except Exception as exc:
        logger.exception("llm.run() fout: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    model_name = os.environ.get("OLLAMA_MODEL", "sovereign-link")

    if req.stream:
        return StreamingResponse(
            _sse_stream(reply, model_name),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 11435))
    logger.info("Sovereign Link API op http://localhost:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
