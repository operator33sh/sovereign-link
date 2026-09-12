"""
tts.py — Text-to-Speech synthesis for Sovereign Link

Uses edge-tts (Microsoft Edge TTS, free, no API key) to synthesise Luna's
replies into audio. Output is converted to OGG OPUS via ffmpeg for Telegram
voice messages; falls back to MP3 if ffmpeg is unavailable.

Environment variables:
    TTS_VOICE   — edge-tts voice name (default: nl-NL-FennaNeural)
"""

import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger(__name__)

TTS_VOICE = os.environ.get("TTS_VOICE", "nl-NL-FennaNeural")


async def synthesize(text: str, prefer_mp3: bool = False) -> str | None:
    """
    Synthesise *text* to speech and return a path to a temporary audio file.

    prefer_mp3=False (default): returns OGG OPUS when ffmpeg is available
                                (Telegram voice messages require OGG).
    prefer_mp3=True:            returns MP3 directly without OGG conversion
                                (browsers play MP3 natively).

    Returns None on failure or empty input.
    The caller is responsible for deleting the returned file.
    """
    if not text or not text.strip():
        return None

    try:
        import edge_tts
    except ImportError:
        logger.error("edge-tts not installed — run: pip install edge-tts")
        return None

    clean = _strip_markdown(text)
    if not clean.strip():
        return None

    tmp_mp3 = tempfile.mktemp(suffix=".mp3")
    tmp_ogg = tempfile.mktemp(suffix=".ogg")

    try:
        communicate = edge_tts.Communicate(clean, TTS_VOICE)
        await communicate.save(tmp_mp3)
    except Exception:
        logger.exception("edge-tts synthesis failed for voice: %s", TTS_VOICE)
        for p in (tmp_mp3, tmp_ogg):
            _safe_remove(p)
        return None

    if prefer_mp3:
        return tmp_mp3

    # Convert MP3 → OGG OPUS (Telegram's native voice format)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", tmp_mp3,
                "-c:a", "libopus", "-b:a", "32k",
                "-vbr", "on", "-compression_level", "10",
                tmp_ogg,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and os.path.getsize(tmp_ogg) > 0:
            _safe_remove(tmp_mp3)
            return tmp_ogg
        logger.warning(
            "ffmpeg OGG conversion failed (rc=%d) — falling back to MP3",
            result.returncode,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not found — sending MP3 instead of OGG OPUS")
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timed out — falling back to MP3")
    except Exception:
        logger.exception("ffmpeg conversion error")

    _safe_remove(tmp_ogg)
    return tmp_mp3  # fallback


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _strip_markdown(text: str) -> str:
    """Remove markdown, emojis, and symbols so TTS reads clean natural language."""
    # Remove fenced code blocks entirely (not speakable)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    # Remove inline code
    text = re.sub(r"`[^`]+`", "", text)
    # Bold / italic / strikethrough (keep inner text)
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    # ATX headings (## Title → Title)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Unordered list markers and bullet symbols
    text = re.sub(r"^[\-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[•◦▸▪▫◆◇●○■□►▻]", "", text)
    # Ordered list markers
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # Markdown links [label](url) → label
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # Bare URLs
    text = re.sub(r"https?://\S+", "", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Hashtags: #woord → woord
    text = re.sub(r"#(\w+)", r"\1", text)
    # @mentions
    text = re.sub(r"@\w+", "", text)
    # Unicode emoji (broad coverage: emoticons, misc symbols, transport, flags, etc.)
    text = re.sub(
        r"[\U0001F300-\U0001F9FF"   # misc symbols, emoticons, transport, activities, objects
        r"\U0001FA00-\U0001FAFF"    # chess, medical, etc.
        r"\U00002702-\U000027B0"    # dingbats
        r"\U0000200D"               # zero-width joiner
        r"\U000024C2-\U0001F251"    # enclosed alphanumerics
        r"\U0000FE00-\U0000FE0F"    # variation selectors
        r"\U0001F1E0-\U0001F1FF"    # flags
        r"\U00002600-\U000026FF"    # misc symbols (☀, ⛔, etc.)
        r"]+",
        " ", text,
    )
    # Text emoticons: :), :(, :D, :-), ;), etc.
    text = re.sub(r"[:;=8][\-o\*\']?[\)\(\[\]dDpP\/\\\|\@\{3\}oO0]", "", text)
    # Collapse multiple blank lines / extra spaces
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
