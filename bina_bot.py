"""
MaklerAssistant — persistent Telegram bot for bina.az.

Runs continuously on your VM (not GitHub Actions). Gives you tappable inline
buttons instead of manually triggering a workflow, and caches the login
session so you only enter an OTP when the session has actually expired.

    python bina_bot.py

Requires (in .env or the environment):
    BOT_TOKEN            from @BotFather
    ALLOWED_USER_IDS     your numeric Telegram id (comma-separated for more)
    BINA_PHONE           optional; your bina.az number (any format)
    HEADLESS=true        keep true on a server
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import bina_core
from bina_core import BinaSession, LoginError, mask

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("makler")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
BINA_PHONE = os.getenv("BINA_PHONE", "").strip()
OTP_TIMEOUT = int(os.getenv("OTP_TIMEOUT", "300"))

# one live BinaSession per phone number, kept warm between actions
_sessions: dict[str, BinaSession] = {}
# per-chat OTP relay (mirrors the pattern from the login test)
_otp_waiters: dict[int, asyncio.Future] = {}
# per-chat single-job lock
_locks: dict[int, asyncio.Lock] = {}


def get_session(phone: str) -> BinaSession:
    if phone not in _sessions:
        _sessions[phone] = BinaSession(phone)
    return _sessions[phone]


def lock_for(chat_id: int) -> asyncio.Lock:
    return _locks.setdefault(chat_id, asyncio.Lock())


# --------------------------------------------------------------------------
class Flow(StatesGroup):
    ask_phone = State()
    ask_otp = State()


class Whitelist(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id not in ALLOWED:
            if isinstance(event, Message):
                await event.answer("⛔️ Private bot.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ Private bot.", show_alert=True)
            return None
        return await handler(event, data)


# --------------------------------------------------------------------------
def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Login / check session", callback_data="login")
    kb.button(text="📋 My ads", callback_data="myads")
    kb.button(text="🩺 Session status", callback_data="status")
    kb.button(text="🚪 Forget session", callback_data="logout")
    kb.adjust(1)
    return kb.as_markup()


def phone_for(state_phone: str | None) -> str | None:
    return state_phone or (BINA_PHONE or None)


# --------------------------------------------------------------------------
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    who = mask(BINA_PHONE) if BINA_PHONE else "not set"
    await msg.answer(
        "👋 <b>MaklerAssistant</b>\n\n"
        f"Configured number: <b>{who}</b>\n\n"
        "Tap a button to begin. Your session is cached, so after the first "
        "login you usually won't need an SMS code again for a while.",
        reply_markup=main_menu(),
    )


@dp.message(Command("cancel"))
async def cancel(msg: Message, state: FSMContext):
    fut = _otp_waiters.get(msg.chat.id)
    if fut and not fut.done():
        fut.cancel()
    await state.clear()
    await msg.answer("Cancelled.", reply_markup=main_menu())


# ---- OTP relay: any digits while we're waiting go to the login coroutine ---
@dp.message(Flow.ask_otp, F.text.regexp(r"^\s*\d[\d\s\-]{2,9}\s*$"))
async def got_otp(msg: Message):
    fut = _otp_waiters.get(msg.chat.id)
    if fut and not fut.done():
        fut.set_result(msg.text)
        # Acknowledge FIRST, then delete — so the code never looks "lost".
        await msg.answer("🔑 Code received, thanks.")
        try:
            await msg.delete()
        except Exception:
            pass


@dp.message(Flow.ask_phone)
async def got_phone(msg: Message, state: FSMContext, bot: Bot):
    await state.update_data(phone=msg.text.strip())
    await state.clear()
    await run_login(bot, msg.chat.id, msg.from_user.id, msg.text.strip(), state)


# ---- buttons ----
@dp.callback_query(F.data == "login")
async def cb_login(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    phone = phone_for(None)
    if not phone:
        await state.set_state(Flow.ask_phone)
        await call.message.answer("📱 Send your bina.az number "
                                  "(e.g. <code>0557778899</code>):")
        return
    await run_login(bot, call.message.chat.id, call.from_user.id, phone, state)


@dp.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery):
    await call.answer()
    phone = phone_for(None)
    if not phone:
        await call.message.answer("No number configured yet.", reply_markup=main_menu())
        return
    sess = get_session(phone)
    saved = sess.session_file.exists()
    await call.message.answer(
        f"<b>{mask(phone)}</b>\n"
        f"Saved session: {'🟢 yes' if saved else '⚪️ none (will need SMS)'}",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "logout")
async def cb_logout(call: CallbackQuery):
    await call.answer("Session forgotten")
    phone = phone_for(None)
    if phone:
        sess = get_session(phone)
        sess.forget()
        await sess.close()
    await call.message.answer("🚪 Saved session cleared. Next login needs an SMS code.",
                              reply_markup=main_menu())


@dp.callback_query(F.data == "myads")
async def cb_myads(call: CallbackQuery, bot: Bot, state: FSMContext):
    await call.answer()
    phone = phone_for(None)
    if not phone:
        await call.message.answer("Configure a number first (🔑 Login).")
        return
    chat_id = call.message.chat.id
    if lock_for(chat_id).locked():
        await call.message.answer("⏳ Busy — one action at a time.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, phone, state)
        if not ok:
            return
        sess = get_session(phone)
        await call.message.answer("📋 You're logged in. (Ad-listing readout is the "
                                  "next feature to add — the session is ready for it.)",
                                  reply_markup=main_menu())


# --------------------------------------------------------------------------
async def _otp_provider(bot: Bot, chat_id: int, state: FSMContext):
    async def provider(phone: str, attempt: int, error: str | None) -> str:
        lines = []
        if error:
            lines.append(f"⚠️ {error}")
        lines.append(f"📲 bina.az texted a code to <b>{mask(phone)}</b>")
        lines.append(f"Send it here (attempt {attempt}/{bina_core.OTP_MAX_ATTEMPTS}). /cancel to stop.")
        await bot.send_message(chat_id, "\n".join(lines))
        await state.set_state(Flow.ask_otp)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _otp_waiters[chat_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=OTP_TIMEOUT)
        finally:
            _otp_waiters.pop(chat_id, None)
            await state.clear()
    return provider


async def ensure_login(bot: Bot, chat_id: int, phone: str, state: FSMContext) -> bool:
    sess = get_session(phone)
    async with sess.lock:
        try:
            fresh = await sess.ensure_logged_in(await _otp_provider(bot, chat_id, state))
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ No code arrived in time.", reply_markup=main_menu())
            return False
        except LoginError as exc:
            await bot.send_message(chat_id, f"❌ {exc}", reply_markup=main_menu())
            return False
        except Exception as exc:
            log.exception("login error")
            await bot.send_message(chat_id, f"💥 {exc}", reply_markup=main_menu())
            return False
    if not fresh:
        await bot.send_message(chat_id, "✅ Already logged in (used the saved session — no SMS needed).")
    return True


async def run_login(bot: Bot, chat_id: int, user_id: int, phone: str, state: FSMContext):
    if lock_for(chat_id).locked():
        await bot.send_message(chat_id, "⏳ Busy — one action at a time.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, phone, state)
        if ok:
            await bot.send_message(
                chat_id,
                f"✅ <b>Logged in</b> as {mask(phone)}.\n"
                "Session saved — future actions should skip the SMS step.",
                reply_markup=main_menu(),
            )


# --------------------------------------------------------------------------
async def main():
    problems = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN missing")
    if not ALLOWED:
        problems.append("ALLOWED_USER_IDS missing")
    if problems:
        raise SystemExit("Cannot start: " + "; ".join(problems))

    bina_core.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.message.middleware(Whitelist())
    dp.callback_query.middleware(Whitelist())
    log.info("MaklerAssistant starting. Allowed: %s", ALLOWED)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        for s in _sessions.values():
            await s.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
