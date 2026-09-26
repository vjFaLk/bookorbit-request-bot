"""BookOrbit request bot – Telegram front end for BookOrbit book requests."""

from __future__ import annotations

import asyncio
import functools
import html
import logging
import os
import re
import sys
import tempfile
from difflib import SequenceMatcher
from typing import Any

from telegram import BotCommand, Chat, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from bot.bookorbit import BookOrbit, BookOrbitError, pick_best

logger = logging.getLogger(__name__)

TERMINAL = {"available", "rejected", "cancelled", "failed"}
ALLOWED_IDS: set[int] = set()
ALLOWED_GROUP_IDS: set[int] = set()
WAIT_MINUTES = int(os.environ.get("REQUEST_WAIT_MINUTES", "30"))
POLL_SECONDS = 15
REVIEWER = "Wangla"
TELEGRAM_MAX_UPLOAD = 50 * 1024 * 1024  # Bot API limit for send_document
FORMAT_ORDER = ("epub", "kepub", "azw3", "mobi", "pdf", "cbz", "cbr")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BIND_HINT = "No email bound to your Telegram account. Send <code>/bind you@kindle.com</code> first."
FOLLOWING = {
    None: f"⏳ Following for up to {WAIT_MINUTES} min.",
    "email": f"⏳ Following for up to {WAIT_MINUTES} min, will email when available.",
    "download": f"⏳ Following for up to {WAIT_MINUTES} min, will send the file when available.",
}
orbit: BookOrbit


def esc(s: Any) -> str:
    return html.escape(str(s or ""))


# -- access ------------------------------------------------------------------


async def role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """'admin' (ALLOWED_USER_IDS, or nothing configured), 'member' (allowed group, or a live member of one in DM), or None."""
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None:
        return None
    if not (ALLOWED_IDS or ALLOWED_GROUP_IDS) or user.id in ALLOWED_IDS:
        return "admin"
    if chat.type != Chat.PRIVATE:
        return "member" if chat.id in ALLOWED_GROUP_IDS else None
    # ponytail: uncached membership lookup per command; cache by user id with a TTL if it shows up in logs
    for gid in ALLOWED_GROUP_IDS:
        try:
            if (await context.bot.get_chat_member(gid, user.id)).status not in ("left", "kicked"):
                return "member"
        except TelegramError:
            logger.warning("Membership check for %s in %s failed", user.id, gid, exc_info=True)
    return None


def guarded(members: bool = False):
    """Access check (admins always; group members only if `members`), plus group etiquette:
    react to the trigger and answer in the user's DM."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            message = update.effective_message
            who = await role(update, context)
            if who is None:
                await message.reply_text("⛔ Access denied.")
                return
            if who == "member" and not members:
                await message.reply_text("⛔ That's admin-only. Use /download or /email.")
                return
            in_group = update.effective_chat.type != Chat.PRIVATE
            if in_group:
                try:
                    await message.set_reaction("👀")
                except TelegramError:
                    logger.debug("Could not react in %s", update.effective_chat.id, exc_info=True)
            try:
                await func(update, context)
            except Forbidden:
                if not in_group:
                    raise
                await message.reply_text(
                    f"👋 I can't message you yet. Start me first: https://t.me/{context.bot.username}?start=1 — then retry here."
                )

        return wrapper

    return decorator


async def dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    return await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)


async def edit(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str) -> None:
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# -- pure helpers ------------------------------------------------------------


def titles_from(text: str) -> list[str]:
    """Strip a leading /command and return one query per non-empty line."""
    if text.startswith("/"):
        text = text.split(maxsplit=1)[1] if " " in text or "\n" in text else ""
    return [line.strip() for line in text.splitlines() if line.strip()]


def bound_recipient(recipients: list[dict[str, Any]], user_id: int) -> dict[str, Any] | None:
    """The BookOrbit recipient bound to a Telegram user: its name is 'tg:<id>' or 'tg:<id> <first name>'."""
    tag = f"tg:{user_id}"
    return next((r for r in recipients if (r.get("name") or "") == tag or (r.get("name") or "").startswith(tag + " ")), None)


def device_type(email: str) -> str:
    return "kindle" if email.lower().endswith(("@kindle.com", "@free.kindle.com")) else "other"


def pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best e-book file to send: epub first, then the rest of FORMAT_ORDER; audio, images and sidecars never."""
    ranked = [f for f in files if (f.get("format") or "").lower() in FORMAT_ORDER]
    return min(ranked, key=lambda f: FORMAT_ORDER.index(f["format"].lower()), default=None)


def norm(text: Any, authors: list[str] | None = None) -> str:
    """Lower-case, drop author names and punctuation, collapse spaces: '1984 - George Orwell' -> '1984'."""
    t = str(text or "").lower()
    for a in authors or []:
        if a:
            t = t.replace(a.lower(), " ")
    return " ".join(re.sub(r"[^\w\s]", " ", t).split())


def find_owned(candidate: dict[str, Any], books: list[dict[str, Any]], threshold: float = 0.9) -> dict[str, Any] | None:
    """Library book whose normalised title is near-identical to the candidate's. When both sides name
    authors one must agree; when the search row lists formats, one must be an e-book format."""
    title = norm(candidate.get("title") or candidate.get("displayTitle"), candidate.get("authors"))
    authors = [norm(a) for a in candidate.get("authors") or [] if a]
    if not title:
        return None

    def score(b: dict[str, Any]) -> float:
        b_authors = [norm(a) for a in b.get("authors") or [] if a]
        if authors and b_authors and not any(SequenceMatcher(None, x, y).ratio() >= 0.8 for x in authors for y in b_authors):
            return 0.0
        formats = b.get("formats")
        if isinstance(formats, list) and not any(str(f or "").lower() in FORMAT_ORDER for f in formats):
            return 0.0
        return SequenceMatcher(None, title, norm(b.get("title"), b.get("authors"))).ratio()

    best = max(books, key=score, default=None)
    return best if best is not None and score(best) >= threshold else None


ICONS = {
    "pending": "🕐", "approved": "✅", "searching": "🔎", "grabbed": "📥", "downloading": "⬇️",
    "importing": "📦", "needs_review": "👀", "available": "📗", "failed": "❌", "rejected": "🚫", "cancelled": "🚫",
}


def describe(item: dict[str, Any]) -> str:
    authors = ", ".join(item.get("authors") or [])
    head = f"<b>{esc(item.get('title'))}</b>" + (f" — {esc(authors)}" if authors else "")
    status = item.get("status", "?")
    reason = item.get("statusReason") or ""
    ref = f"#{item['id']} · " if item.get("id") else ""
    line = ref + esc(status).replace("_", " ") + (f" — {esc(reason)}" if reason else "")
    text = f"{ICONS.get(status, '📚')} {head}\n{line}"
    if status == "needs_review":
        text += f"\n👀 Needs a human look — reach out to {REVIEWER}."
    return text


# -- request flow ------------------------------------------------------------


async def owned_book_id(candidate: dict[str, Any]) -> int | None:
    """Library check before filing: BookOrbit's exact ISBN13/title match, then a fuzzy pass over a
    library search for the cleaned title. A failed check never blocks a request."""
    try:
        if book_id := await orbit.owned_book_id(candidate):
            return book_id
        q = norm(candidate.get("title") or candidate.get("displayTitle"), candidate.get("authors")) or (candidate.get("authors") or [""])[0]
        owned = find_owned(candidate, await orbit.search_library(q)) if q else None
        return owned["id"] if owned else None
    except BookOrbitError:
        logger.warning("Availability check failed for %r", candidate.get("title"), exc_info=True)
        return None


async def submit(query: str, deliver: str | None, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await dm(context, user_id, f"🔍 {esc(query)}")
    try:
        candidates = await orbit.search(query)
        if not candidates:
            await edit(context, user_id, msg.message_id, f"📭 No metadata found for <i>{esc(query)}</i>.")
            return
        best = pick_best(query, candidates)
        if book_id := await owned_book_id(best):
            # Already in the library: no request, deliver (or just say so) right away.
            item = {"title": best.get("title") or best.get("displayTitle"), "authors": best.get("authors"), "status": "available", "matchedBookId": book_id}
            await edit(context, user_id, msg.message_id, describe(item) + "\n📚 Already in the library, no request filed.")
            if deliver:
                context.application.create_task(deliver_book(item, user_id, msg.message_id, context, deliver))
            return
        result = await orbit.create_request(best)
    except BookOrbitError as exc:
        await edit(context, user_id, msg.message_id, f"❌ {esc(exc)}")
        return

    item = result["request"]
    text = describe(item) + (" (already requested, you were added)" if result.get("subscribed") else "")
    done = item["status"] in TERMINAL
    if not done:
        text += "\n" + FOLLOWING[deliver]
    await edit(context, user_id, msg.message_id, text)
    if done:
        if deliver:
            context.application.create_task(deliver_book(item, user_id, msg.message_id, context, deliver))
    else:
        context.application.create_task(follow_request(item["id"], item["status"], user_id, msg.message_id, context, deliver))


async def follow_request(
    request_id: int, status: str, chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE, deliver: str | None
) -> None:
    """Poll the request, edit the message on every status change, and deliver the book at the end if asked."""
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
            await edit(context, chat_id, message_id, describe(item) + ("" if status in TERMINAL else "\n" + FOLLOWING[deliver]))
        if status in TERMINAL:
            break
    else:
        await edit(context, chat_id, message_id, f"{describe(item)}\n⏳ Stopped following after {WAIT_MINUTES} min.")
        return
    if deliver:
        await deliver_book(item, chat_id, message_id, context, deliver)


async def deliver_book(item: dict[str, Any], chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    """Email the matched book to the user's bound recipient, or send it as a Telegram document."""
    book_id = item.get("matchedBookId")
    if item.get("status") != "available":
        text = f"⚠️ Not sending {describe(item)}\nIt never became available."
    elif not book_id:
        text = f"⚠️ {describe(item)}\nIt was fulfilled outside the library, so I can't fetch the file. Ask {REVIEWER}."
    else:
        try:
            text = await (email_book if mode == "email" else download_book)(item, book_id, chat_id, context)
        except BookOrbitError as exc:
            text = f"❌ {'Email' if mode == 'email' else 'Download'} failed for {describe(item)}\n{esc(exc)}"
    await edit(context, chat_id, message_id, text)


async def email_book(item: dict[str, Any], book_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    rcpt = bound_recipient(await orbit.recipients(), user_id)
    if not rcpt:
        return f"❌ {describe(item)}\n{BIND_HINT}"
    providers = await orbit.providers()
    if not providers:
        raise BookOrbitError("No email provider configured in BookOrbit.")
    provider = next((p for p in providers if p.get("isDefault")), providers[0])
    await orbit.send_email(book_id, rcpt["id"], provider["id"])
    return f"📧 {describe(item)}\nSent to {esc(rcpt['email'])}. Delivery can take a few minutes; on a Kindle, tap Sync to fetch it."


async def download_book(item: dict[str, Any], book_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    file = pick_file((await orbit.book(book_id)).get("files") or [])
    if not file:
        return f"⚠️ {describe(item)}\nThat book has no e-book file."
    size = file.get("sizeBytes") or 0
    if size > TELEGRAM_MAX_UPLOAD:
        return f"⚠️ {describe(item)}\nThe file is {size / 1e6:.0f} MB and Telegram bots can send at most 50 MB. Use /email instead."
    with tempfile.NamedTemporaryFile(suffix=f".{file.get('format') or 'bin'}") as tmp:
        await orbit.download_file(file["id"], tmp.name)
        with open(tmp.name, "rb") as fh:
            await context.bot.send_document(
                chat_id=user_id, document=fh, filename=file.get("filename") or f"{item.get('title')}.{file.get('format')}",
                read_timeout=300, write_timeout=300,
            )
    return f"📎 {describe(item)}\nSent as a file below."


# -- handlers ----------------------------------------------------------------


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE, deliver: str | None = None) -> None:
    user_id = update.effective_user.id
    titles = titles_from(update.effective_message.text or "")
    if not titles:
        await dm(context, user_id, "Send a title (one per line), e.g.\n/request Dune\nNeuromancer")
        return
    if deliver == "email":
        try:
            if not bound_recipient(await orbit.recipients(), user_id):
                await dm(context, user_id, f"❌ {BIND_HINT}")
                return
        except BookOrbitError as exc:
            await dm(context, user_id, f"❌ {esc(exc)}")
            return
    # ponytail: sequential and unbounded; cap if batches get large
    for title in titles:
        await submit(title, deliver, user_id, context)


@guarded()
async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, context)


@guarded(members=True)
async def email_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, context, deliver="email")


@guarded(members=True)
async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(update, context, deliver="download")


@guarded(members=True)
async def bind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bind <email>: create or update the BookOrbit recipient bound to this Telegram user. /bind alone shows it."""
    user = update.effective_user
    email = (context.args or [""])[0].strip().lower()
    try:
        recipients = await orbit.recipients()
        mine = bound_recipient(recipients, user.id)
        if not email:
            text = f"📮 Bound to {esc(mine['email'])}." if mine else f"❌ {BIND_HINT}"
        elif not EMAIL_RE.match(email):
            text = f"❌ That doesn't look like an email address: {esc(email)}"
        elif any(r is not mine and (r.get("email") or "").lower() == email for r in recipients):
            text = "❌ That email is already bound to another Telegram account."
        else:
            fields = {"name": f"tg:{user.id} {user.first_name or ''}".strip()[:255], "email": email, "deviceType": device_type(email)}
            if mine:
                await orbit.update_recipient(mine["id"], **fields)
            else:
                await orbit.create_recipient(**fields)
            text = f"📮 Bound to {esc(email)}. /email will send there."
    except BookOrbitError as exc:
        text = f"❌ {esc(exc)}"
    await dm(context, user.id, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📚 <b>BookOrbit Request Bot</b>\n\n"
        "/request &lt;title&gt; — request a book and follow its progress (admins only)\n"
        "/email &lt;title&gt; — request, then email it to your bound address once it lands\n"
        "/download &lt;title&gt; — request, then send the file here once it lands\n"
        "/bind &lt;email&gt; — bind your Kindle/email address for /email\n"
        "/help — this message\n\n"
        "In a DM a plain message works like /request. One title per line requests several at once.\n"
        "From a group, commands work too; I answer in your DM (start me here first).",
        parse_mode=ParseMode.HTML,
    )


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("request", "Request a book"),
        BotCommand("email", "Request a book and email it when available"),
        BotCommand("download", "Request a book and send the file when available"),
        BotCommand("bind", "Bind your email address for /email"),
        BotCommand("help", "Show help"),
    ])


async def post_shutdown(app: Application) -> None:
    await orbit.close()


def ids(var: str) -> set[int]:
    return {int(x) for x in os.environ.get(var, "").split(",") if x.strip()}


def main() -> None:
    global orbit
    env = {k: os.environ.get(k, "") for k in ("TELEGRAM_BOT_TOKEN", "BOOKORBIT_URL", "BOOKORBIT_USERNAME", "BOOKORBIT_PASSWORD")}
    if missing := [k for k, v in env.items() if not v]:
        sys.exit(f"ERROR: missing environment variables: {', '.join(missing)}")
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=os.environ.get("LOG_LEVEL", "INFO").upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token in the URL
    ALLOWED_IDS.update(ids("ALLOWED_USER_IDS"))
    ALLOWED_GROUP_IDS.update(ids("ALLOWED_GROUP_IDS"))

    orbit = BookOrbit(env["BOOKORBIT_URL"], env["BOOKORBIT_USERNAME"], env["BOOKORBIT_PASSWORD"])
    logger.info(
        "BookOrbit: %s · allowed users: %s · allowed groups: %s",
        env["BOOKORBIT_URL"], sorted(ALLOWED_IDS) or "-", sorted(ALLOWED_GROUP_IDS) or "-",
    )

    app = ApplicationBuilder().token(env["TELEGRAM_BOT_TOKEN"]).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("request", request_command))
    app.add_handler(CommandHandler("email", email_command))
    app.add_handler(CommandHandler("download", download_command))
    app.add_handler(CommandHandler("bind", bind_command))
    app.add_handler(CommandHandler(["help", "start"], help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, request_command))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
