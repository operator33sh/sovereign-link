"""vision_analyze.py — image analysis tool for the Sovereign-Link agent harness.

Supports HTTP/HTTPS URLs, local file paths, and base64-encoded image strings.
Resizes images larger than 2 MB before sending to the vision model.
"""

from __future__ import annotations

import base64
import io
import logging
import os

import requests
from PIL import Image

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "Provide a detailed visual description of this image, "
    "perform OCR on any text or memes present, and explain its context."
)
_MAX_BYTES = 10 * 1024 * 1024   # 10 MB download cap
_RESIZE_THRESHOLD = 2 * 1024 * 1024  # 2 MB → trigger resize
_MAX_DIMENSION = 1920

# Magic-byte MIME detection (jpeg, png, gif, webp)
_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),   # checked together with offset 8 below
]


def _detect_mime(data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    raise ValueError(f"Unsupported image format (first bytes: {data[:4].hex()})")


def _resize_if_needed(data: bytes, mime: str) -> tuple[bytes, str]:
    """Return (possibly resized) bytes and (possibly updated) mime type."""
    if len(data) <= _RESIZE_THRESHOLD:
        return data, mime
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    scale = _MAX_DIMENSION / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def _load_image(image_source: str) -> tuple[bytes, str]:
    """Resolve image_source to (raw_bytes, mime_type)."""
    if image_source.startswith("http://") or image_source.startswith("https://"):
        headers = {"User-Agent": "SovereignLink/1.0 (vision_analyze)"}
        resp = requests.get(image_source, headers=headers, timeout=10, stream=True)
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > _MAX_BYTES:
                raise ValueError(f"Image exceeds {_MAX_BYTES // 1024 // 1024} MB limit")
            chunks.append(chunk)
        data = b"".join(chunks)
    elif os.path.isfile(image_source):
        with open(image_source, "rb") as f:
            data = f.read()
    else:
        # Assume base64
        try:
            data = base64.b64decode(image_source)
        except Exception as exc:
            raise ValueError(f"image_source is not a valid URL, file path, or base64 string: {exc}") from exc

    mime = _detect_mime(data)
    data, mime = _resize_if_needed(data, mime)
    return data, mime


def _call_vision_llm(image_b64: str, mime: str, prompt: str) -> str:
    """Call the vision model directly without touching the conversation context."""
    import llm as _llm
    from datetime import datetime
    from timezone_manager import get_zoneinfo as _tz

    timestamp = datetime.now(tz=_tz()).strftime("%Y-%m-%d %H:%M:%S")
    system = f"{_llm._build_system_prompt()}\n\nCurrent date and time: {timestamp}."

    payload = {
        "model": _llm.VISION_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt, "images": [image_b64]},
        ],
        "stream": False,
    }
    response = _llm._client.post("/api/chat", json=payload)
    response.raise_for_status()
    return response.json()["message"].get("content", "").strip()


def vision_analyze(args: dict) -> str:
    image_source = (args.get("image_source") or "").strip()
    if not image_source:
        return "Error: 'image_source' parameter is required."

    prompt = (args.get("prompt") or "").strip() or _DEFAULT_PROMPT

    try:
        data, mime = _load_image(image_source)
    except Exception as exc:
        logger.warning("vision_analyze: failed to load image: %s", exc)
        return f"Error loading image: {exc}"

    image_b64 = base64.b64encode(data).decode()

    try:
        return _call_vision_llm(image_b64, mime, prompt)
    except Exception as exc:
        logger.exception("vision_analyze: LLM call failed")
        return f"Error analyzing image: {exc}"


DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "vision_analyze",
            "description": (
                "Analyze an image from a URL, local file path, or base64 string. "
                "Returns OCR text, scene description, and contextual understanding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_source": {
                        "type": "string",
                        "description": "HTTP/HTTPS URL, absolute local file path, or base64-encoded image string.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What to look for in the image. Default: detailed description + OCR.",
                    },
                },
                "required": ["image_source"],
            },
        },
    }
]

HANDLERS: dict[str, callable] = {
    "vision_analyze": lambda args: vision_analyze(args),
}
