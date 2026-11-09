"""Telegram frontend for the WhatsApp report bot."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import db
import wa

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("aiomysql").setLevel(logging.WARNING)


@dataclass
class Job:
    chat_id: int
    user_id: int
    number: str
    total: int
    done: int = 0
    success: int = 0
    failed: int = 0
    cancel: bool = False
    message_id: int | None = None
    shots: int = 0
    started: float = 0.0
    db_id: int = 0


queue: asyncio.Queue = asyncio.Queue()
current: Job | None = None
state = {"linked": None}
app = None
loop = None


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_health() -> None:
    server = HTTPServer(("0.0.0.0", config.PORT), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def start_keepalive() -> None:
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url or not config.KEEPALIVE_SECONDS:
        return

    def loop_ping():
        while True:
            try:
                urllib.request.urlopen(url, timeout=30)
            except Exception:
                pass
            time.sleep(config.KEEPALIVE_SECONDS)

    threading.Thread(target=loop_ping, daemon=True).start()


MENU = "👋 WhatsApp Report Bot\n\nChoose an option below:\n\n🔹 Developed by Marvel"


def menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Reporting", callback_data="report")],
        [InlineKeyboardButton("🔗 Link via Pair Code", callback_data="pair")],
        [InlineKeyboardButton("📷 Link via QR Code", callback_data="qr")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("🧾 History", callback_data="history")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
        [InlineKeyboardButton("📝 Help", callback_data="help")],
    ])


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]])


def cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel", callback_data="cancel")]])


async def edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=markup)


def linked_label() -> str:
    return {
        True: "yes",
        False: "no — link first",
        None: "unknown (relink if reporting fails)",
    }[state["linked"]]


async def track_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    ctx.user_data["uid"] = user.id
    await db.upsert_user(user.id, user.username, user.first_name)


async def blocked(update: Update, ctx) -> bool:
    if await db.is_banned(update.effective_user.id):
        target = update.effective_message
        if target:
            await target.reply_text("🚫 You are banned from using this bot.")
        return True
    return False


async def cmd_start(update: Update, _ctx):
    await update.message.reply_text(MENU, reply_markup=menu_kb())


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("step"):
        ctx.user_data["step"] = None
        await update.message.reply_text("✅ Cancelled.", reply_markup=menu_kb())
    elif current and not current.cancel:
        current.cancel = True
        await update.message.reply_text("🛑 Stopping after the current report…")
    else:
        await update.message.reply_text("Nothing running.", reply_markup=menu_kb())


async def cmd_status(update: Update, _ctx):
    await update.message.reply_text(await status_text(update.effective_user.id), reply_markup=back_kb())


async def status_text(user_id: int) -> str:
    used = await db.daily_count(user_id)
    quota = f"{used}/{config.DAILY_LIMIT}" if config.DAILY_LIMIT else f"{used} (unlimited)"
    lines = [
        "📊 Status",
        "",
        f"🔗 WhatsApp linked: {linked_label()}",
        f"🟢 Job running: {'yes' if current else 'no'}",
        f"⏳ Queued jobs: {queue.qsize()}",
        f"📅 Reports today: {quota}",
        f"🗄 Database: {'connected' if db.enabled() else 'offline'}",
    ]
    if current:
        lines += [
            f"📞 Number: +{current.number}",
            f"🔄 This run: {current.success} ok / {current.failed} failed of {current.done}",
        ]
    lines += ["", "🔹 Developed by Marvel"]
    return "\n".join(lines)


async def cmd_history(update: Update, _ctx):
    await update.message.reply_text(await history_text(update.effective_user.id), reply_markup=back_kb())


async def history_text(user_id: int) -> str:
    rows = await db.recent_jobs(user_id, 10)
    if not rows:
        return "🧾 No runs recorded yet."
    out = ["🧾 Last 10 runs", ""]
    for row in rows:
        stamp = row["created_at"].strftime("%d %b %H:%M") if row["created_at"] else "—"
        out.append(
            f"• +{row['number']} — {row['success']}/{row['total']} ok — {row['status']} — {stamp}"
        )
    return "\n".join(out)


async def cmd_stats(update: Update, _ctx):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Admin only.")
        return
    data = await db.totals()
    top = await db.top_users(5)
    out = [
        "🗄 Global stats",
        "",
        f"👥 Users: {data['users']}",
        f"🎯 Jobs: {data['jobs']}",
        f"✅ Successful reports: {data['success']}",
        f"🟢 Running now: {data['running']}",
    ]
    if top:
        out += ["", "🏆 Top users"]
        out += [f"• {r['username'] or r['user_id']} — {r['total_reports']}" for r in top]
    await update.message.reply_text("\n".join(out))


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    uid = int(ctx.args[0])
    await db.upsert_user(uid, None, None)
    await db.set_banned(uid, True)
    await update.message.reply_text(f"🚫 Banned {uid}")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    await db.set_banned(int(ctx.args[0]), False)
    await update.message.reply_text(f"✅ Unbanned {ctx.args[0]}")


async def cmd_browser(update: Update, _ctx):
    if current:
        await update.message.reply_text("⏳ Wait for the current job to finish.")
        return
    await update.message.reply_text("♻️ Restarting the browser…")
    await asyncio.to_thread(wa.reset_driver)
    state["linked"] = None
    await update.message.reply_text("✅ Browser restarted.", reply_markup=menu_kb())


async def cmd_relink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["step"] = "pair"
    await update.message.reply_text(
        "🔗 Send YOUR WhatsApp number with country code (e.g. 2348012345678).\n"
        "I'll return a code you type into your phone.",
        reply_markup=back_kb(),
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if await blocked(update, ctx):
        return

    if data == "menu":
        await edit(query, MENU, menu_kb())

    elif data == "report":
        if queue.qsize() >= config.MAX_QUEUE:
            await edit(query, f"⏳ Queue is full ({config.MAX_QUEUE} jobs). Try again shortly.", back_kb())
            return
        if config.DAILY_LIMIT:
            used = await db.daily_count(update.effective_user.id)
            if used >= config.DAILY_LIMIT:
                await edit(query, f"📅 Daily limit reached ({used}/{config.DAILY_LIMIT}). Resets at midnight.", back_kb())
                return
        ctx.user_data["step"] = "report"
        await edit(
            query,
            "📲 Send the number to report, with country code.\n\n"
            "Example: 2349049611574\n\n"
            f"Optional count: `2349049611574 100` (default {config.REPORTS_PER_RUN}, max {config.MAX_REPORTS}).",
            back_kb(),
        )

    elif data == "pair":
        if state["linked"] is True:
            await edit(query, "✅ Already linked — just start reporting.", menu_kb())
            return
        ctx.user_data["step"] = "pair"
        await edit(
            query,
            "🔗 Send YOUR WhatsApp number with country code (e.g. 2348012345678).\n"
            "I'll return a code you type into your phone.",
            back_kb(),
        )

    elif data == "qr":
        await edit(query, "📱 Opening WhatsApp Web for a QR code…")
        asyncio.create_task(qr_flow(query.message.chat_id))

    elif data == "status":
        await edit(query, await status_text(update.effective_user.id), back_kb())

    elif data == "history":
        await edit(query, await history_text(update.effective_user.id), back_kb())

    elif data == "about":
        await edit(
            query,
            "🤖 Automated WhatsApp reporting\n\n"
            f"Default reports per run: {config.REPORTS_PER_RUN}\n"
            f"Delay between reports: {config.DELAY}s\n"
            f"Daily limit: {config.DAILY_LIMIT or 'unlimited'}\n"
            "One WhatsApp session, jobs run in order.\n\n"
            "🔹 Developed by Marvel",
            back_kb(),
        )

    elif data == "help":
        await edit(
            query,
            "❓ How to use\n\n"
            "1️⃣ Link once:\n"
            "   • 🔗 Pair code — send your number, then enter the code in\n"
            "     WhatsApp → Linked devices → Link a device →\n"
            "     'Link with phone number instead'\n"
            "   • or 📷 scan the QR\n"
            "2️⃣ 🚀 Start Reporting and send the target number\n"
            "3️⃣ /cancel stops after the current report\n"
            "4️⃣ 📊 Status, 🧾 History, /relink to link again, /browser to restart Chromium\n\n"
            "🔹 Developed by Marvel",
            back_kb(),
        )

    elif data == "cancel":
        if current:
            current.cancel = True


INPUT_RE = re.compile(r"^\s*\+?([\d\s\-()]{8,25})(?:\s+(\d{1,4}))?\s*$")


def parse_input(text: str):
    match = INPUT_RE.match(text)
    if not match:
        return None, None
    digits = re.sub(r"\D", "", match.group(1))
    if not 8 <= len(digits) <= 15:
        return None, None
    count = int(match.group(2)) if match.group(2) else config.REPORTS_PER_RUN
    return digits, max(1, min(count, config.MAX_REPORTS))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    step = ctx.user_data.get("step")
    if not step:
        return
    if await blocked(update, ctx):
        return

    number, count = parse_input(update.message.text)
    if not number:
        await update.message.reply_text("❌ Not a valid number. Send digits with country code, e.g. 2349049611574")
        return
    ctx.user_data["step"] = None

    if step == "pair":
        await update.message.reply_text(f"🔗 Generating a pair code for +{number}…")
        asyncio.create_task(pair_flow(update.effective_chat.id, number))
        return

    if config.DAILY_LIMIT:
        used = await db.daily_count(update.effective_user.id)
        remaining = max(0, config.DAILY_LIMIT - used)
        if remaining == 0:
            await update.message.reply_text(f"📅 Daily limit reached ({used}/{config.DAILY_LIMIT}).")
            return
        if count > remaining:
            count = remaining
            await update.message.reply_text(f"ℹ️ Trimmed to {count} — that's your remaining daily allowance.")

    await queue.put(
        Job(chat_id=update.effective_chat.id, user_id=update.effective_user.id, number=number, total=count)
    )
    position = queue.qsize()
    await update.message.reply_text(
        f"✅ Queued +{number} — {count} reports (position {position})."
        + ("" if position == 1 else "\nYou'll be notified when it starts.")
    )


async def pair_flow(chat_id: int, number: str):
    try:
        driver = await asyncio.to_thread(wa.get_driver)
    except wa.DriverBroken as exc:
        await app.bot.send_message(chat_id, f"⚠️ Browser failed: {exc}", reply_markup=back_kb())
        return

    try:
        code = await asyncio.to_thread(wa.pair_code, driver, number)
    except Exception as exc:
        log.warning("pair flow crashed: %s", exc)
        code = None

    if code == "ALREADY_LINKED":
        state["linked"] = True
        await db.set_linked(True, number)
        await app.bot.send_message(chat_id, "✅ Already linked — no code needed.", reply_markup=menu_kb())
        return

    if not code:
        png = await asyncio.to_thread(wa.screenshot, driver)
        if png:
            try:
                await app.bot.send_photo(chat_id, png, caption="❌ Pair code failed — this is the current screen.")
            except Exception:
                pass
        await app.bot.send_message(
            chat_id, "❌ Couldn't generate a code. Try 📷 QR instead, or /browser then retry.", reply_markup=menu_kb()
        )
        return

    await app.bot.send_message(
        chat_id,
        f"🔑 PAIR CODE:  {code}\n\n"
        "On your phone now:\n"
        "1. WhatsApp → ⚙️ Settings\n"
        "2. Linked devices → Link a device\n"
        "3. 'Link with phone number instead'\n"
        f"4. Type:  {code}\n\n"
        "⏱️ About 2 minutes.",
    )

    if await asyncio.to_thread(wa.wait_linked, driver, config.PAIR_TIMEOUT):
        state["linked"] = True
        await db.set_linked(True, number)
        await app.bot.send_message(chat_id, "✅ Linked. You can start reporting.", reply_markup=menu_kb())
    else:
        await app.bot.send_message(chat_id, "❌ Timed out. Request a fresh code.", reply_markup=menu_kb())


async def qr_flow(chat_id: int):
    try:
        driver = await asyncio.to_thread(wa.get_driver)
    except wa.DriverBroken as exc:
        await app.bot.send_message(chat_id, f"⚠️ Browser failed: {exc}", reply_markup=back_kb())
        return

    await app.bot.send_message(
        chat_id, "📷 Phone: WhatsApp → Linked devices → Link a device.\nScan the code I send next — codes expire fast."
    )

    def send_qr(png, attempt):
        try:
            asyncio.run_coroutine_threadsafe(
                app.bot.send_photo(chat_id, png, caption=f"📷 QR {attempt}/{config.QR_ATTEMPTS} — scan now"), loop
            ).result(30)
        except Exception as exc:
            log.warning("QR send failed: %s", exc)

    ok = await asyncio.to_thread(wa.qr_login, driver, send_qr)
    if ok:
        state["linked"] = True
        await db.set_linked(True)
        await app.bot.send_message(chat_id, "✅ Linked via QR. Start reporting.", reply_markup=menu_kb())
    else:
        await app.bot.send_message(
            chat_id, "❌ QR login failed. Pair code is more reliable — use 🔗 instead.", reply_markup=menu_kb()
        )


async def worker(_app):
    while True:
        job = await queue.get()
        try:
            await run_job(job)
        except Exception:
            log.exception("job crashed")
        finally:
            queue.task_done()


async def run_job(job: Job):
    global current
    current = job
    job.started = time.time()
    job.db_id = await db.create_job(job.chat_id, job.user_id, job.number, job.total)

    msg = await app.bot.send_message(job.chat_id, "🔎 Preparing browser…", reply_markup=cancel_kb())
    job.message_id = msg.message_id
    last_edit = [0.0]

    async def progress(text: str, force: bool = False):
        now = time.time()
        if not force and now - last_edit[0] < 3.5:
            return
        last_edit[0] = now
        try:
            await app.bot.edit_message_text(
                text, chat_id=job.chat_id, message_id=job.message_id, reply_markup=cancel_kb()
            )
        except Exception:
            pass

    try:
        driver = await asyncio.to_thread(wa.get_driver)
    except wa.DriverBroken as exc:
        current = None
        await db.finish_job(job.db_id, "failed", "browser failed")
        await app.bot.edit_message_text(
            f"⚠️ Browser failed: {exc}", chat_id=job.chat_id, message_id=job.message_id, reply_markup=back_kb()
        )
        return

    await db.mark_running(job.db_id)
    await progress(f"✅ Browser ready — reporting +{job.number} {job.total}×", force=True)

    consecutive = 0
    while job.done < job.total and not job.cancel:
        try:
            ok, reason = await asyncio.to_thread(wa.report_once, driver, job.number)
        except wa.LoginRequired:
            state["linked"] = False
            await db.set_linked(False)
            current = None
            await db.finish_job(job.db_id, "failed", "session expired")
            await app.bot.edit_message_text(
                f"⚠️ WhatsApp session expired after {job.done} reports.\nUse /relink, then run again.",
                chat_id=job.chat_id, message_id=job.message_id, reply_markup=back_kb(),
            )
            return
        except wa.DriverBroken as exc:
            log.warning("recovering browser: %s", exc)
            try:
                driver = await asyncio.to_thread(wa.recover_driver)
            except wa.DriverBroken:
                current = None
                await db.finish_job(job.db_id, "failed", "browser crash")
                await app.bot.edit_message_text(
                    "⚠️ Browser keeps crashing. Try /browser, or lower REPORTS_PER_RUN.",
                    chat_id=job.chat_id, message_id=job.message_id, reply_markup=back_kb(),
                )
                return
            continue

        job.done += 1
        if ok:
            job.success += 1
            consecutive = 0
        else:
            job.failed += 1
            consecutive += 1
            log.info("report %s/%s failed: %s", job.done, job.total, reason)
            if config.DEBUG_SHOTS and job.shots < 3:
                png = await asyncio.to_thread(wa.screenshot, driver)
                if png:
                    job.shots += 1
                    try:
                        await app.bot.send_photo(job.chat_id, png, caption=f"⚠️ #{job.done}: {reason}")
                    except Exception:
                        pass
            if consecutive >= 5:
                try:
                    driver = await asyncio.to_thread(wa.recover_driver)
                except wa.DriverBroken:
                    pass
                consecutive = 0

        if config.DB_LOG_REPORTS:
            await db.log_report(job.db_id, job.number, ok, reason)
        await db.update_job(job.db_id, job.done, job.success, job.failed)
        await progress(f"🔄 {job.done}/{job.total} — ✅ {job.success} ok, ❌ {job.failed} failed")

        if job.done < job.total and not job.cancel:
            await asyncio.sleep(config.DELAY)

    state["linked"] = True
    await db.add_user_reports(job.user_id, job.success)
    await db.finish_job(job.db_id, "cancelled" if job.cancel else "done")
    current = None

    elapsed = int(time.time() - job.started)
    header = "🛑 Cancelled" if job.cancel else "✅ Done"
    await app.bot.edit_message_text(
        f"{header}\n\n"
        f"Number: +{job.number}\n"
        f"Reports sent: {job.success}\n"
        f"Failed: {job.failed}\n"
        f"Attempts: {job.done}/{job.total}\n"
        f"Time: {elapsed // 60}m {elapsed % 60}s\n\n"
        "🔹 Developed by Marvel",
        chat_id=job.chat_id, message_id=job.message_id, reply_markup=back_kb(),
    )


async def post_init(application):
    global app, loop
    app = application
    loop = asyncio.get_running_loop()
    await db.init()
    await db.abandon_stale_jobs()
    state["linked"] = None
    application.create_task(worker(application))
    log.info("worker started")


async def post_shutdown(_application):
    await asyncio.to_thread(wa.shutdown)
    await db.close()


def validate_token(token: str):
    url = f"https://api.telegram.org/bot{token}/getMe"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "unknown error"))
    return data["result"]["username"]


async def on_error(_update, ctx):
    log.error("handler error", exc_info=ctx.error)


def main():
    if not config.TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    try:
        username = validate_token(config.TOKEN)
    except Exception as exc:
        raise SystemExit(f"Telegram rejected the token: {exc}")

    log.info("token valid — bot is @%s", username)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    start_health()
    start_keepalive()

    application = (
        Application.builder()
        .token(config.TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(TypeHandler(Update, track_user), group=-1)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("history", cmd_history))
    application.add_handler(CommandHandler("relink", cmd_relink))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("ban", cmd_ban))
    application.add_handler(CommandHandler("unban", cmd_unban))
    application.add_handler(CommandHandler("browser", cmd_browser))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)

    log.info("polling Telegram…")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
