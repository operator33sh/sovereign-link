import logging
import os
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import context
import llm
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

    await update.message.chat.send_action("typing")

    timestamp = datetime.now()
    file_name = f"vault_{timestamp.strftime('%Y_%m_%d_%H%M')}.md"

    # Take last 10 messages (5 exchanges) from session history
    recent = context.get_history()[-10:]
    if not recent:
        await update.message.reply_text("No conversation history to summarize.")
        return

    try:
        summary = llm.summarize_to_vault(recent)
    except Exception as e:
        logger.exception("Summarize error")
        await update.message.reply_text(f"Error generating summary: {e}")
        return

    note = f"## {timestamp.strftime('%Y-%m-%d %H:%M:%S')} — {file_name}\n\n{summary}\n"

    write_result = write_vault(file_name, note)
    sync_result = sync_vault()

    await update.message.reply_text(
        f"Vault note saved to `{file_name}`.\n\n{write_result}\n{sync_result}",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    user_text = update.message.text
    logger.info("Received message: %s", user_text[:80])

    # Send typing indicator
    await update.message.chat.send_action("typing")

    try:
        reply = llm.run(user_text)
    except Exception as e:
        logger.exception("LLM error")
        reply = f"Error: {e}"

    await update.message.reply_text(reply)


async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Check if the bot is online"),
        BotCommand("clear", "Clear the current session context"),
        BotCommand("vault", "Save last 5 exchanges as a vault note"),
    ])


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_set_commands).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("vault", cmd_vault))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
