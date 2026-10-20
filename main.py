import logging
import asyncio
from dotenv import load_dotenv

load_dotenv()

from bot import build_app
from vector import start_vault_watcher

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    logger.info("Starting Sovereign-Link bot...")

    # Load Whisper on the main thread before any other threads start: a lazy
    # first load inside a worker thread crashed the whole process with
    # "Intel oneMKL FATAL ERROR: Cannot load libctranslate2" (not catchable).
    try:
        import llm
        llm._get_whisper()
        logger.info("Whisper model preloaded")
    except Exception:
        logger.exception("Whisper preload failed — voice messages will retry lazily")

    start_vault_watcher()

    # Fix for Python 3.14+ event loop issues
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = build_app()
    app.run_polling(drop_pending_updates=True)
