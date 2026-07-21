import asyncio
import base64
import logging
import os
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import context
import llm
import vector
from tools import write_vault, sync_vault

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])


def _is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text("Sovereign-Link online.")


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
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
    date_str = timestamp.strftime("%Y-%m-%d")
    time_str = timestamp.strftime("%H-%M")
    file_name = f"{date_str}_{time_str}_{titel}.md"

    note = f"## {timestamp.strftime('%Y-%m-%d %H:%M')} — {titel.replace('-', ' ')}\n\n{samenvatting}\n"

    write_result = write_vault(file_name, note)
    sync_result = sync_vault()

    await update.message.reply_text(
        f"Vault notitie opgeslagen als `{file_name}`.\n\n{write_result}\n{sync_result}",
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

    await status.edit_text(reply)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

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

    await update.message.reply_text(reply)


async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Check if the bot is online"),
        BotCommand("clear", "Clear the current session context"),
        BotCommand("vault", "Save last 5 exchanges as a vault note"),
        BotCommand("whisper", "Generate a tweet from a random vault insight"),
    ])


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_set_commands).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("vault", cmd_vault))
    app.add_handler(CommandHandler("whisper", cmd_whisper))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
