import logging
import asyncio
from dotenv import load_dotenv

load_dotenv()

from bot import build_app
from vector import start_vault_watcher

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    logger.info("Starting Sovereign-Link bot...")

    start_vault_watcher()

    # Fix for Python 3.14+ event loop issues
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = build_app()
    app.run_polling(drop_pending_updates=True)
