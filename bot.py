import asyncio
import base64
import html
import logging
import os
import re
from datetime import datetime

AUDIO_TMP_DIR = "/tmp/audio_transcription"

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import context
import llm
import vector
from tools import write_vault, sync_vault, generate_time_tag
from memory_manager import run_memory_pipeline
from agent import run_system_check
import chat_bridge
from session_logger import session_logger
from scheduler import scheduler as _scheduler
from automations import automation_engine as _automation_engine
from proactive import user_status, proactive_dispatcher
from notifications import notification_manager


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Silence chatty HTTP client loggers — every LLM/embed call otherwise floods the console
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "90"))  # seconds before llm.run() is considered hung

AUTO_MEMORY_EVERY = 10  # trigger memory pipeline every N user messages
_message_count = 0
_messages_since_last_memory = 0

SESSION_DRAFT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "system_memory", "session_draft.md"
)

# Stored by handle_message so the proactive dispatcher can push messages
# even when no active handler is running.
_proactive_loop: "asyncio.AbstractEventLoop | None" = None


def _save_session_draft() -> None:
    """Write raw conversation transcript to system_memory as a crash-safe draft.

    Written directly to the filesystem (not via write_vault) so it is never
    indexed by the VectorDB pipeline.
    """
    history = context.get_history()
    if not history:
        return
    lines = ["# Session Draft (auto-generated, safe to delete)\n"]
    for m in history:
        if m.get("content") and isinstance(m["content"], str):
            lines.append(f"**{m['role'].upper()}:** {m['content']}\n")
    try:
        os.makedirs(os.path.dirname(SESSION_DRAFT_PATH), exist_ok=True)
        with open(SESSION_DRAFT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def _delete_session_draft() -> None:
    """Remove the session draft after a proper memory save."""
    try:
        if os.path.exists(SESSION_DRAFT_PATH):
            os.remove(SESSION_DRAFT_PATH)
    except Exception:
        pass


def _strip_timestamps(text: str) -> str:
    result = re.sub(r"\n\[[^\]]{10,}\]", "", text).strip()
    return result or "…"


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

    from timezone_manager import get_zoneinfo as _get_local_tz
    timestamp = datetime.now(tz=_get_local_tz())

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


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    global _message_count, _messages_since_last_memory, _proactive_loop
    _message_count += 1
    _messages_since_last_memory += 1

    # Track activity (sleep mode is NOT cleared by user messages)
    user_status.update_activity(update.effective_chat.id)
    _proactive_loop = asyncio.get_event_loop()

    # Typing indicator immediately so the user knows transcription has started
    async def keep_typing():
        while True:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())

    try:
        if update.message.voice:
            tg_file_id = update.message.voice.file_id
            extension = ".ogg"
        else:
            audio = update.message.audio
            tg_file_id = audio.file_id
            extension = os.path.splitext(audio.file_name or ".mp3")[1] or ".mp3"

        os.makedirs(AUDIO_TMP_DIR, exist_ok=True)
        tg_file = await ctx.bot.get_file(tg_file_id)
        tmp_path = os.path.join(AUDIO_TMP_DIR, f"{tg_file_id}{extension}")
        await tg_file.download_to_drive(tmp_path)

        transcription = await asyncio.to_thread(llm.transcribe_audio, tmp_path)
        try:
            os.remove(tmp_path)
        except Exception:
            pass

        if not transcription:
            typing_task.cancel()
            await update.message.reply_text("Kon geen tekst uit het audiobericht halen.")
            return

        _loop = asyncio.get_event_loop()
        chat_bridge.set_sender(_make_sender(update, _loop))
        chat_bridge.set_context_injector(_sleep_aware_context_injector)
        chat_bridge.set_llm_trigger(_make_llm_trigger_fn())

        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(llm.run, transcription),
                timeout=LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("LLM run() timed out na %.0fs voor audiobericht", LLM_TIMEOUT)
            reply = "Het antwoord duurde te lang. Probeer het opnieuw."

        typing_task.cancel()
        await update.message.reply_text(
            f"<i>{html.escape(transcription)}</i>",
            parse_mode="HTML",
        )
        stripped = _strip_timestamps(reply)
        if stripped:
            await update.message.reply_text(stripped)
        _save_session_draft()
        session_logger.on_turn(context.get_history())

    except Exception as e:
        logger.exception("Audio transcription error")
        typing_task.cancel()
        await update.message.reply_text(f"Fout bij verwerken audiobericht: {e}")


def _sleep_aware_context_injector(text: str) -> None:
    """Context injector that redirects agent notifications to the queue during sleep mode."""
    if user_status.is_sleeping():
        try:
            notification_manager.write(
                content=text,
                agent_id="agent",
                priority="medium",
                category="task",
            )
        except Exception:
            logger.exception("Sleep-mode: failed to queue agent notification")
    else:
        context.add_message("user", text)


def _make_sender(update: "Update", loop: "asyncio.AbstractEventLoop") -> "Callable[[str], None]":
    """
    Create a thread-safe Telegram sender with empty-text guard and delivery error logging.
    Captures update + loop at call time so agent threads can send without holding a reference.
    """
    def _sender(text: str) -> None:
        if not text or not text.strip():
            logger.warning("Sender: leeg bericht onderschept, niet verstuurd naar Telegram")
            return
        fut = asyncio.run_coroutine_threadsafe(
            update.message.reply_text(text[:4096]), loop
        )
        def _check(f: "asyncio.Future") -> None:
            exc = f.exception()
            if exc:
                logger.error("Sender: Telegram bezorging mislukt: %s", exc)
        fut.add_done_callback(_check)
    return _sender


def _make_llm_trigger_fn() -> "Callable[[], None]":
    """
    Create a sync no-arg callable that triggers an autonomous LLM call
    and pushes the result to Telegram via the registered chat sender.
    Called from daemon agent threads, so must be fully synchronous.
    Suppressed during sleep mode — agent completions are queued instead.
    """
    def _trigger() -> None:
        try:
            if user_status.is_sleeping():
                logger.info("LLM trigger suppressed: sleep mode active")
                return
            response = llm.run_triggered()
            if response:
                sender = chat_bridge.get_sender()
                if sender:
                    sender(response[:4096])
        except Exception:
            logger.exception("Autonomous LLM trigger failed")
    return _trigger


async def _auto_memory_background(update: Update, transcript: str) -> None:
    """Fire-and-forget background task: run memory pipeline silently."""
    try:
        result = await asyncio.to_thread(run_memory_pipeline, transcript)
        _delete_session_draft()
        logger.info("Sovereign Memory auto-saved: %s", result['file_name'])
    except Exception:
        logger.exception("Background auto-memory pipeline error")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return

    global _message_count, _messages_since_last_memory, _proactive_loop
    _message_count += 1
    _messages_since_last_memory += 1

    # Track activity (sleep mode is NOT cleared by user messages)
    user_status.update_activity(update.effective_chat.id)
    _proactive_loop = asyncio.get_event_loop()

    if _messages_since_last_memory >= AUTO_MEMORY_EVERY:
        _messages_since_last_memory = 0  # reset immediately to prevent double-trigger
        history = context.get_history()
        transcript = "\n".join(
            f"{m['role'].upper()}: {m.get('content', '')}"
            for m in history
            if m.get("content") and isinstance(m["content"], str)
        )
        asyncio.create_task(_auto_memory_background(update, transcript))

    user_text = update.message.text

    async def keep_typing():
        while True:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    # Register sender + context injector so agents can report back to chat and Luna's context
    _loop = asyncio.get_event_loop()
    chat_bridge.set_sender(_make_sender(update, _loop))
    chat_bridge.set_context_injector(_sleep_aware_context_injector)
    chat_bridge.set_llm_trigger(_make_llm_trigger_fn())

    typing_task = asyncio.create_task(keep_typing())
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(llm.run, user_text),
            timeout=LLM_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("LLM run() timed out na %.0fs voor bericht: %r", LLM_TIMEOUT, user_text[:100])
        reply = "Het antwoord duurde te lang. Probeer het opnieuw."
    except Exception as e:
        logger.exception("LLM error")
        reply = f"Error: {e}"
    finally:
        typing_task.cancel()

    stripped = _strip_timestamps(reply)
    if stripped:
        await update.message.reply_text(stripped)
    else:
        logger.warning("LLM run(): leeg antwoord voor bericht: %r", user_text[:100])
    _save_session_draft()
    session_logger.on_turn(context.get_history())


async def _run_syscheck_background(update: Update) -> None:
    """Fire-and-forget: run the system check agent and notify the user."""
    try:
        result = await asyncio.to_thread(run_system_check)
        summary = result[:3800] if len(result) > 3800 else result
        await update.message.reply_text(
            f"System check complete:\n\n{summary}",
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("System check agent error")
        await update.message.reply_text("System check failed. Check the vault agent log.")


async def cmd_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Spawn a background agent with a custom goal: /agent <goal description>"""
    if not _is_authorized(update):
        return

    goal = " ".join(ctx.args or []).strip()
    if not goal:
        await update.message.reply_text(
            "Usage: `/agent <goal description>`\n\nExample: `/agent Analyseer de vault en schrijf een overzicht van alle inzichten over zelfzorg`",
            parse_mode="Markdown",
        )
        return

    agent_name = f"Agent_{datetime.now().strftime('%H%M%S')}"
    await update.message.reply_text(
        f"Agent `{agent_name}` gestart.\nDoel: _{goal}_\n\nLuna deelt de bevindingen zodra het klaar is.",
        parse_mode="Markdown",
    )

    # Register callbacks so Luna can synthesize and report when the agent completes
    _loop = asyncio.get_event_loop()
    chat_bridge.set_sender(_make_sender(update, _loop))
    chat_bridge.set_context_injector(_sleep_aware_context_injector)
    chat_bridge.set_llm_trigger(_make_llm_trigger_fn())

    from agent import launch_agent
    launch_agent(
        goal, agent_name,
        context_injector=chat_bridge.get_context_injector(),
        llm_trigger=chat_bridge.get_llm_trigger(),
    )


async def cmd_timezone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    from timezone_manager import set_timezone, get_timezone_info
    args = ctx.args
    if not args:
        await update.message.reply_text(get_timezone_info())
        return
    tz_string = " ".join(args).strip()
    result = set_timezone(tz_string)
    await update.message.reply_text(result)


async def cmd_sleep(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable sleep mode: /sleep  (disables proactive pushes for medium/low notifications)"""
    if not _is_authorized(update):
        return
    await update.message.reply_text(user_status.set_sleep_mode(True))


async def cmd_wake(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable sleep mode: /wake"""
    if not _is_authorized(update):
        return
    await update.message.reply_text(user_status.set_sleep_mode(False))


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List scheduled tasks: /schedule [all]"""
    if not _is_authorized(update):
        return
    include_done = "all" in (ctx.args or [])
    text = _scheduler.list_tasks(include_done)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_syscheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the built-in system health check agent."""
    if not _is_authorized(update):
        return

    await update.message.reply_text(
        "System Check Agent gestart. Ik scan tools, test LLM-redenering en vault-connectiviteit. Even geduld…",
    )
    asyncio.create_task(_run_syscheck_background(update))


async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Check if the bot is online"),
        BotCommand("clear", "Clear the current session context"),
        BotCommand("vault", "Save last 5 exchanges as a vault note"),
        BotCommand("memory", "Extract and save sovereign memory log from conversation"),
        BotCommand("whisper", "Generate a tweet from a random vault insight"),
        BotCommand("agent", "Spawn a background agent with a custom goal"),
        BotCommand("syscheck", "Run system health check agent"),
        BotCommand("schedule", "List scheduled tasks (/schedule all for history)"),
        BotCommand("sleep", "Enable sleep mode — hold non-urgent notifications"),
        BotCommand("wake", "Disable sleep mode — resume proactive notifications"),
        BotCommand("timezone", "Get or set local timezone (/timezone Europe/Amsterdam)"),
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
    pipeline_ok = False
    try:
        result = await asyncio.to_thread(run_memory_pipeline, transcript)
        logger.info("Shutdown memory saved: %s", result["file_name"])
        pipeline_ok = True
    except Exception:
        logger.exception("Shutdown memory pipeline failed")

    if not pipeline_ok:
        # Fallback: raw session snapshot only if the memory pipeline failed.
        session_logger.force_flush(context.get_history(), reason="shutdown")


def build_app() -> Application:
    _scheduler.start()
    _automation_engine.start()
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).post_init(_set_commands).post_shutdown(_on_shutdown).build()

    # Wire proactive dispatcher: send_fn uses app.bot.send_message so it can
    # push to the chat from background threads without an active update context.
    def _proactive_send(text: str) -> None:
        loop = _proactive_loop
        chat_id = user_status.get_chat_id()
        if loop is None or chat_id is None:
            logger.warning("ProactiveDispatcher: no loop/chat_id yet — message queued for next interaction")
            return
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id, text[:4096], parse_mode="Markdown"),
            loop,
        )

    proactive_dispatcher.set_send_fn(_proactive_send)
    notification_manager._on_write = proactive_dispatcher.notify
    proactive_dispatcher.start()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("vault", cmd_vault))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("whisper", cmd_whisper))
    app.add_handler(CommandHandler("agent", cmd_agent))
    app.add_handler(CommandHandler("syscheck", cmd_syscheck))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("sleep", cmd_sleep))
    app.add_handler(CommandHandler("wake", cmd_wake))
    app.add_handler(CommandHandler("timezone", cmd_timezone))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
