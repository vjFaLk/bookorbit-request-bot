"""BookOrbit request bot – Telegram front end for BookOrbit book requests."""

from __future__ import annotations

import asyncio
import functools
import html
import logging
import os
import sys
from typing import Any

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from bot.bookorbit import BookOrbit, BookOrbitError, pick_best

logger = logging.getLogger(__name__)

TERMINAL = {"available", "rejected", "cancelled", "failed"}
ALLOWED_IDS: set[int] = set()
WAIT_MINUTES = int(os.environ.get("REQUEST_WAIT_MINUTES", "30"))
POLL_SECONDS = 15
orbit: BookOrbit


def esc(s: Any) -> str:
    return html.escape(str(s or ""))


def restricted(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if ALLOWED_IDS and (update.effective_user is None or update.effective_user.id not in ALLOWED_IDS):
            await update.effective_message.reply_text("⛔ Access denied.")
            return
        return await func(update, context)

    return wrapper


def titles_from(text: str) -> list[str]:
    """Strip a leading /command and return one query per non-empty line."""
    if text.startswith("/"):
        text = text.split(maxsplit=1)[1] if " " in text or "\n" in text else ""
    return [line.strip() for line in text.splitlines() if line.strip()]


ICONS = {
    "pending": "🕐", "approved": "✅", "searching": "🔎", "grabbed": "📥", "downloading": "⬇️",
    "importing": "📦", "needs_review": "👀", "available": "📗", "failed": "❌", "rejected": "🚫", "cancelled": "🚫",
}


def describe(item: dict[str, Any]) -> str:
    authors = ", ".join(item.get("authors") or [])
    head = f"<b>{esc(item.get('title'))}</b>" + (f" — {esc(authors)}" if authors else "")
    status = item.get("status", "?")
    reason = item.get("statusReason") or ("check the Book Dock" if status == "needs_review" else "")
    line = f"#{item.get('id')} · {esc(status).replace('_', ' ')}" + (f" — {esc(reason)}" if reason else "")
    return f"{ICONS.get(status, '📚')} {head}\n{line}"


async def submit(query: str, email: bool, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text(f"🔍 {query}")
    try:
        candidates = await orbit.search(query)
        if not candidates:
            await msg.edit_text(f"📭 No metadata found for <i>{esc(query)}</i>.", parse_mode=ParseMode.HTML)
            return
        result = await orbit.create_request(pick_best(query, candidates))
    except BookOrbitError as exc:
        await msg.edit_text(f"❌ {esc(exc)}", parse_mode=ParseMode.HTML)
        return

    item = result["request"]
    chat_id = update.effective_chat.id
    text = describe(item) + (" (already requested, you were added)" if result.get("subscribed") else "")
    if item["status"] in TERMINAL:
        if email:
            context.application.create_task(send_book(item, chat_id, context))
    else:
        text += f"\n⏳ Following for up to {WAIT_MINUTES} min" + (", will email when available." if email else ".")
        context.application.create_task(follow_request(item["id"], item["status"], chat_id, context, email))
    await msg.edit_text(text, parse_mode=ParseMode.HTML)


async def follow_request(request_id: int, status: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, email: bool) -> None:
    """Poll the request, report every status change, and email the book at the end if asked."""
    item: dict[str, Any] = {"id": request_id, "status": status}
    for _ in range(WAIT_MINUTES * 60 // POLL_SECONDS):
        await asyncio.sleep(POLL_SECONDS)
        try:
            item = await orbit.get_request(request_id)
        except BookOrbitError:
            logger.warning("Polling request %s failed", request_id, exc_info=True)
            continue
        if item["status"] != status:
            status = item["status"]
            await context.bot.send_message(chat_id=chat_id, text=describe(item), parse_mode=ParseMode.HTML)
        if status in TERMINAL:
            break
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=f"{describe(item)}\n⏳ Stopped following after {WAIT_MINUTES} min.", parse_mode=ParseMode.HTML
        )
        return
    if email:
        await send_book(item, chat_id, context)


async def send_book(item: dict[str, Any], chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Email the request's matched book to the first recipient."""
    book_id = item.get("matchedBookId")
    if item.get("status") != "available" or not book_id:
        text = f"⚠️ Not emailing {describe(item)}\nIt never became available."
    else:
        try:
            recipients, providers = await orbit.recipients(), await orbit.providers()
            if not recipients:
                raise BookOrbitError("No email recipients configured in BookOrbit.")
            if not providers:
                raise BookOrbitError("No email provider configured in BookOrbit.")
            # ponytail: "first" = the default one if flagged, else the first listed
            rcpt = next((r for r in recipients if r.get("isDefault")), recipients[0])
            provider = next((p for p in providers if p.get("isDefault")), providers[0])
            await orbit.send_email(book_id, rcpt["id"], provider["id"])
            text = f"📧 {describe(item)}\nSent to {esc(rcpt.get('name') or rcpt.get('email'))}."
        except BookOrbitError as exc:
            text = f"❌ Email failed for {describe(item)}\n{esc(exc)}"
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE, email: bool = False) -> None:
    titles = titles_from(update.effective_message.text or "")
    if not titles:
        await update.effective_message.reply_text("Send a title (one per line), e.g.\n/request Dune\nNeuromancer")
        return
    # ponytail: sequential and unbounded; cap if batches get large
    for title in titles:
        await submit(title, email, update, context)


@restricted
async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, context)


@restricted
async def email_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, context, email=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📚 <b>BookOrbit Request Bot</b>\n\n"
        "/request &lt;title&gt; — request a book and follow its progress\n"
        "/email &lt;title&gt; — request, then email it to your first recipient once it lands\n"
        "/help — this message\n\n"
        "A plain message works like /request. One title per line requests several at once.",
        parse_mode=ParseMode.HTML,
    )


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("request", "Request a book"),
        BotCommand("email", "Request a book and email it when available"),
        BotCommand("help", "Show help"),
    ])


async def post_shutdown(app: Application) -> None:
    await orbit.close()


def main() -> None:
    global orbit
    env = {k: os.environ.get(k, "") for k in ("TELEGRAM_BOT_TOKEN", "BOOKORBIT_URL", "BOOKORBIT_USERNAME", "BOOKORBIT_PASSWORD")}
    if missing := [k for k, v in env.items() if not v]:
        sys.exit(f"ERROR: missing environment variables: {', '.join(missing)}")
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=os.environ.get("LOG_LEVEL", "INFO").upper())
    ALLOWED_IDS.update(int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip())

    orbit = BookOrbit(env["BOOKORBIT_URL"], env["BOOKORBIT_USERNAME"], env["BOOKORBIT_PASSWORD"])
    logger.info("BookOrbit: %s · allowed users: %s", env["BOOKORBIT_URL"], sorted(ALLOWED_IDS) or "everyone")

    app = ApplicationBuilder().token(env["TELEGRAM_BOT_TOKEN"]).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("request", request_command))
    app.add_handler(CommandHandler("email", email_command))
    app.add_handler(CommandHandler(["help", "start"], help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, request_command))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
