"""
reaction_bridge.py — Thread-safe bridge between llm thread and Telegram async loop.

bot.py calls configure() before each llm.run() invocation.
tools/reaction_tools.py calls send_reaction() from the llm thread.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_bot: Any | None = None  # telegram.Bot
_chat_id: int | None = None
_last_user_msg_id: int | None = None


def configure(
    loop: asyncio.AbstractEventLoop,
    bot: Any,
    chat_id: int,
    user_msg_id: int,
) -> None:
    """Called from bot.py (async context) before each llm.run() call."""
    global _loop, _bot, _chat_id, _last_user_msg_id
    _loop = loop
    _bot = bot
    _chat_id = chat_id
    _last_user_msg_id = user_msg_id


def send_reaction(emoji: str, message_id: int | None = None) -> str:
    """Place an emoji reaction on a Telegram message (called from llm thread)."""
    if _bot is None or _loop is None or _chat_id is None:
        return "Reactie niet beschikbaar (geen actieve Telegram-verbinding)."

    mid = message_id if message_id is not None else _last_user_msg_id
    if mid is None:
        return "Geen bericht-ID beschikbaar voor reactie."

    from telegram import ReactionTypeEmoji

    async def _do() -> None:
        await _bot.set_message_reaction(
            chat_id=_chat_id,
            message_id=mid,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )

    try:
        future = asyncio.run_coroutine_threadsafe(_do(), _loop)
        future.result(timeout=5)
        return f"Reactie {emoji} geplaatst op bericht #{mid}."
    except Exception as exc:
        logger.warning("send_reaction failed: %s", exc)
        return f"Reactie mislukt: {exc}"
