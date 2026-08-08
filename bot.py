import asyncio
import base64
import logging
import os
import re
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import context
import llm
import vector
from tools import write_vault, sync_vault, generate_time_tag
from memory_manager import run_memory_pipeline


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

AUTO_MEMORY_EVERY = 20  # trigger memory pipeline every N user messages
_message_count = 0
_messages_since_last_memory = 0

SESSION_DRAFT_PATH = "memory/session_draft.md"


def _save_session_draft() -> None:
    """Write raw conversation transcript to vault as a crash-safe draft."""
    history = context.get_history()
    if not history:
        return
    lines = ["# Session Draft (auto-generated, safe to delete)\n"]
    for m in history:
        if m.get("content") and isinstance(m["content"], str):
            lines.append(f"**{m['role'].upper()}:** {m['content']}\n")
    write_vault(SESSION_DRAFT_PATH, "\n".join(lines))


def _delete_session_draft() -> None:
    """Remove the session draft after a proper memory save."""
    import os
    from vector import VAULT_PATH
    path = os.path.join(VAULT_PATH, SESSION_DRAFT_PATH)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _strip_timestamps(text: str) -> str:
    return re.sub(r"\n\[[^\]]{10,}\]", "", text)


def _is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text("Sovereign-Link online.")


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    recent = context.get_history()
    if recent:
        transcript = "\n".join(
            f"{m['role'].upper()}: {m.get('content', '')}"
            for m in recent
            if m.get("content") and isinstance(m["content"], str)
        )

        async def keep_typing():
            while True:
                try:
                    await update.message.chat.send_action("typing")
                except Exception:
                    pass
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(keep_typing())
        memory_note = None
        try:
            memory_result = await asyncio.to_thread(run_memory_pipeline, transcript)
            memory_note = memory_result["file_name"]
            global _messages_since_last_memory
            _messages_since_last_memory = 0
            _delete_session_draft()
        except Exception:
            logger.exception("Auto-memory on clear failed")
        finally:
            typing_task.cancel()

        context.clear()
        msg = "Context cleared."
        if memory_note:
            msg += f"\nSovereign Memory saved: `{memory_note}`"
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        context.clear()
        await update.message.reply_text("Context cleared.")


async def cmd_vault(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    timestamp = datetime.now()

    # Take last 10 messages (5 exchanges) from session history
    recent = context.get_history()[-10:]
    if not recent:
        await update.message.reply_text("Geen gespreksgeschiedenis om samen te vatten.")
        return

    async def keep_typing():
        while True:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        result = await asyncio.to_thread(llm.summarize_to_vault, recent)
    except Exception as e:
        logger.exception("Summarize error")
        typing_task.cancel()
        await update.message.reply_text(f"Fout bij samenvatten: {e}")
        return
    finally:
        typing_task.cancel()

    titel = result.get("titel", "aantekening").strip().replace(" ", "-")
    samenvatting = result.get("samenvatting", "")
    raw_tags = result.get("tags", [])
    tags_str = " ".join(
        t if t.startswith("#") else f"#{t}"
        for t in raw_tags
        if isinstance(t, str) and t.strip()
    )
    date_str = timestamp.strftime("%Y-%m-%d")
    time_str = timestamp.strftime("%H-%M")
    file_name = f"{date_str}_{time_str}_{titel}.md"

    # Find related notes for [[wikilinks]] in graph view
    related_files = await asyncio.to_thread(vector.search_vault_files, samenvatting, 5)
    related_links = [
        f"[[{f[:-3] if f.endswith('.md') else f}]]"
        for f in related_files
        if f != file_name
    ]
    links_str = "  ".join(related_links)

    note = (
        f"## {timestamp.strftime('%Y-%m-%d %H:%M')} {generate_time_tag()} — {titel.replace('-', ' ')}\n\n"
        f"{samenvatting}\n"
        + (f"\n### Zie ook\n{links_str}\n" if links_str else "")
        + (f"\n{tags_str}\n" if tags_str else "")
    )

    write_result = write_vault(file_name, note, timestamp.isoformat())
    sync_result = sync_vault()

    await update.message.reply_text(
        f"Vault notitie opgeslagen als `{file_name}`.\n\n{write_result}\n{sync_result}",
        parse_mode="Markdown",
    )


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    recent = context.get_history()[-20:]
    if not recent:
        await update.message.reply_text("No conversation history to process.")
        return

    transcript = "\n".join(
        f"{m['role'].upper()}: {m.get('content', '')}"
        for m in recent
        if m.get("content") and isinstance(m["content"], str)
    )

    async def keep_typing():
        while True:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        result = await asyncio.to_thread(run_memory_pipeline, transcript)
        global _messages_since_last_memory
        _messages_since_last_memory = 0
        _delete_session_draft()
    except Exception as e:
        logger.exception("Memory pipeline error")
        typing_task.cancel()
        await update.message.reply_text(f"Memory pipeline failed: {e}")
        return
    finally:
        typing_task.cancel()

    await update.message.reply_text(
        f"Memory log saved as `{result['file_name']}`.\n\n{result['write_result']}\n{result['sync_result']}",
        parse_mode="Markdown",
    )


async def cmd_whisper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    chunk = vector.random_chunk()
    if not chunk:
        await update.message.reply_text("Vector index is leeg. Voer eerst ingest.py uit.")
        return

    async def keep_typing():
        while True:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        tweet = await asyncio.to_thread(llm.whisper_tweet, chunk)
    except Exception as e:
        logger.exception("Whisper error")
        typing_task.cancel()
        await update.message.reply_text(f"Fout bij whisper: {e}")
        return
    finally:
        typing_task.cancel()

    await update.message.reply_text(tweet)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    photo = update.message.photo[-1]  # largest available size
    caption = update.message.caption or ""
    logger.info("Received photo file_id=%s caption=%r", photo.file_id, caption[:80])

    status = await update.message.reply_text("Processing image...")

    try:
        tg_file = await ctx.bot.get_file(photo.file_id)
        image_bytes = await tg_file.download_as_bytearray()
    except Exception as e:
        logger.exception("Photo download error")
        await status.edit_text(f"Could not download image: {e}")
        return

    image_b64 = base64.b64encode(image_bytes).decode()

    async def keep_typing():
        while True:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        reply = await asyncio.to_thread(llm.run_with_image, caption, image_b64)
    except Exception as e:
        logger.exception("LLM image error")
        typing_task.cancel()
        await status.edit_text(f"Error: {e}")
        return
    finally:
        typing_task.cancel()

    await status.edit_text(_strip_timestamps(reply))


async def _auto_memory_background(update: Update, transcript: str) -> None:
    """Fire-and-forget background task: run memory pipeline silently."""
    global _messages_since_last_memory
    try:
        result = await asyncio.to_thread(run_memory_pipeline, transcript)
        _messages_since_last_memory = 0
        _delete_session_draft()
        logger.info("Sovereign Memory auto-saved: %s", result['file_name'])
    except Exception:
        logger.exception("Background auto-memory pipeline error")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    global _message_count, _messages_since_last_memory
    _message_count += 1
    _messages_since_last_memory += 1
    if _message_count % AUTO_MEMORY_EVERY == 0:
        history = context.get_history()
        transcript = "\n".join(
            f"{m['role'].upper()}: {m.get('content', '')}"
            for m in history
            if m.get("content") and isinstance(m["content"], str)
        )
        asyncio.create_task(_auto_memory_background(update, transcript))

    user_text = update.message.text
    logger.info("Received message: %s", user_text[:80])

    async def keep_typing():
        while True:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        reply = await asyncio.to_thread(llm.run, user_text)
    except Exception as e:
        logger.exception("LLM error")
        reply = f"Error: {e}"
    finally:
        typing_task.cancel()

    await update.message.reply_text(_strip_timestamps(reply))
    _save_session_draft()


async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Check if the bot is online"),
        BotCommand("clear", "Clear the current session context"),
        BotCommand("vault", "Save last 5 exchanges as a vault note"),
        BotCommand("memory", "Extract and save sovereign memory log from conversation"),
        BotCommand("whisper", "Generate a tweet from a random vault insight"),
    ])


async def _on_shutdown(app: Application) -> None:
    """Save unsaved messages to memory on bot shutdown."""
    if _messages_since_last_memory == 0:
        return
    history = context.get_history()
    if not history:
        return
    transcript = "\n".join(
        f"{m['role'].upper()}: {m.get('content', '')}"
        for m in history
        if m.get("content") and isinstance(m["content"], str)
    )
    if not transcript.strip():
        return
    logger.info("Shutdown: saving %d unsaved messages to Sovereign Memory...", _messages_since_last_memory)
    try:
        result = await asyncio.to_thread(run_memory_pipeline, transcript)
        logger.info("Shutdown memory saved: %s", result["file_name"])
    except Exception:
        logger.exception("Shutdown memory pipeline failed")


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_set_commands).post_shutdown(_on_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("vault", cmd_vault))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("whisper", cmd_whisper))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
