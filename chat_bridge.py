"""
chat_bridge.py — Thread-to-async Telegram sender + LLM context injector registry.

Background agents run in daemon threads and cannot directly await coroutines.
This module holds two references set before each llm.run() call:

  _sender          — pushes a message to the active Telegram chat
                     (wraps asyncio.run_coroutine_threadsafe)

  _context_injector — injects text into context.py as an attributed "user"
                      message so Luna is aware of the agent's output on the
                      next conversation turn.

Single-user design: this bot serves one user (ALLOWED_USER_ID), so globals
are safe — there are no concurrent sessions to mix up.
"""

import logging
from typing import Callable

logger = logging.getLogger(__name__)

_sender: Callable[[str], None] | None = None
_context_injector: Callable[[str], None] | None = None
_llm_trigger: Callable[[], None] | None = None


# ------------------------------------------------------------------
# Telegram sender
# ------------------------------------------------------------------

def set_sender(fn: Callable[[str], None]) -> None:
    """Register the active chat sender. Call this before each llm.run()."""
    global _sender
    _sender = fn


def get_sender() -> Callable[[str], None] | None:
    """Return the current sender, or None if not set."""
    return _sender


def clear_sender() -> None:
    global _sender
    _sender = None


# ------------------------------------------------------------------
# LLM context injector
# ------------------------------------------------------------------

def set_context_injector(fn: Callable[[str], None]) -> None:
    """
    Register a function that injects text into the LLM conversation context.
    Call this before each llm.run().

    In bot.py:
        chat_bridge.set_context_injector(
            lambda text: context.add_message("user", text)
        )
    """
    global _context_injector
    _context_injector = fn


def get_context_injector() -> Callable[[str], None] | None:
    """Return the current context injector, or None if not set."""
    return _context_injector


def clear_context_injector() -> None:
    global _context_injector
    _context_injector = None


# ------------------------------------------------------------------
# Autonomous LLM trigger
# ------------------------------------------------------------------

def set_llm_trigger(fn: Callable[[], None]) -> None:
    """
    Register a no-arg callable that triggers an autonomous LLM call
    after an agent completes. The callable handles fetching a response
    from the LLM and pushing it to chat.
    """
    global _llm_trigger
    _llm_trigger = fn


def get_llm_trigger() -> Callable[[], None] | None:
    """Return the current LLM trigger, or None if not set."""
    return _llm_trigger


def clear_llm_trigger() -> None:
    global _llm_trigger
    _llm_trigger = None
