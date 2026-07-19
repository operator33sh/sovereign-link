import logging
from dotenv import load_dotenv

load_dotenv()

from bot import build_app

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    logger.info("Starting Sovereign-Link bot...")
    app = build_app()
    app.run_polling(drop_pending_updates=True)
