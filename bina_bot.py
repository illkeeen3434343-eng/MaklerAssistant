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
import contextvars
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import bina_core
import security
import users as U
from bina_core import BinaSession, LoginError, mask
from security import OwnershipError

from ask_broker import broker as ask, Cancelled
from bina_publish import PublishFlow, PublishError

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("makler")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BINA_PHONE = os.getenv("BINA_PHONE", "").strip()
OTP_TIMEOUT = int(os.getenv("OTP_TIMEOUT", "300"))

# one live BinaSession per (owner_id, phone), kept warm between actions
_sessions: dict[tuple[int, str], BinaSession] = {}
# per-chat OTP relay (mirrors the pattern from the login test)
_otp_waiters: dict[int, asyncio.Future] = {}
# per-chat single-job lock
_locks: dict[int, asyncio.Lock] = {}


def get_session(owner_id: int, phone: str) -> BinaSession:
    key = (owner_id, security.phone_hash(phone))
    if key not in _sessions:
        _sessions[key] = BinaSession(phone, owner_id)
    return _sessions[key]


def check_ownership(phone: str, owner_id: int) -> bool:
    """True if owner_id may use this number. Claims it if unclaimed."""
    try:
        security.claim(phone, owner_id)
        return True
    except OwnershipError:
        return False


def lock_for(chat_id: int) -> asyncio.Lock:
    return _locks.setdefault(chat_id, asyncio.Lock())


# --------------------------------------------------------------------------
class Flow(StatesGroup):
    ask_phone = State()
    ask_otp = State()


_last_user_id: dict[int, int] = {}   # chat_id -> user_id, filled by middleware


class Whitelist(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user:
            _current_uid.set(user.id)
            _last_user_id[getattr(getattr(event, "chat", None), "id", user.id) or user.id] = user.id
            if user.id not in ALLOWED:
                if isinstance(event, Message):
                    await event.answer("⛔️ Private bot.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⛔️ Private bot.", show_alert=True)
                return None
        return await handler(event, data)


# --------------------------------------------------------------------------
BTN_LOGIN = "🔑 Login"
BTN_NEW = "➕ New listing"
BTN_ADS = "📋 My ads"
BTN_STATUS = "🩺 Status"
BTN_SESSIONS = "📱 Sessions"
BTN_ADMIN = "🛠 Admin"

# sessions sub-menu
BTN_S_NEW = "➕ New session"
BTN_S_SWITCH = "🔀 Switch number"
BTN_S_LIST = "📄 My numbers"
BTN_S_FORGET = "🚪 Forget session"

# admin sub-menu
BTN_A_PENDING = "⏳ Pending users"
BTN_A_USERS = "👥 All users"
BTN_A_SETSTATUS = "✅ Set status"
BTN_A_SETTIER = "⭐ Set tier"
BTN_A_BACK = "⬅️ Back"


_current_uid: contextvars.ContextVar = contextvars.ContextVar("uid", default=None)


def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    uid = user_id if user_id is not None else _current_uid.get()
    rows = [
        [KeyboardButton(text=BTN_NEW)],
        [KeyboardButton(text=BTN_LOGIN), KeyboardButton(text=BTN_SESSIONS)],
        [KeyboardButton(text=BTN_ADS), KeyboardButton(text=BTN_STATUS)],
    ]
    if uid is not None and uid in ADMIN_IDS:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               input_field_placeholder="Tap a button…")


def sessions_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_S_NEW), KeyboardButton(text=BTN_S_SWITCH)],
            [KeyboardButton(text=BTN_S_LIST), KeyboardButton(text=BTN_S_FORGET)],
            [KeyboardButton(text=BTN_A_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Sessions…",
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_A_PENDING), KeyboardButton(text=BTN_A_USERS)],
            [KeyboardButton(text=BTN_A_SETSTATUS), KeyboardButton(text=BTN_A_SETTIER)],
            [KeyboardButton(text=BTN_A_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Admin…",
    )


# Tracks each user's currently-active bina.az number (defaults to BINA_PHONE).
_active_number: dict[int, str] = {}


def active_phone(user_id: int) -> str | None:
    if user_id in _active_number:
        return _active_number[user_id]
    nums = U.numbers(user_id)
    if nums:
        return nums[0]
    return BINA_PHONE or None


def phone_for(state_phone: str | None) -> str | None:
    if state_phone:
        return state_phone
    uid = _current_uid.get()
    if uid is not None:
        p = active_phone(uid)
        if p:
            return p
    return BINA_PHONE or None


# --------------------------------------------------------------------------
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    # Register the user (pending by default). Admins are auto-active.
    U.ensure_user(uid, default_status="active" if uid in ADMIN_IDS else "pending")
    if uid in ADMIN_IDS and not U.is_active(uid):
        U.set_status(uid, "active")
    who = mask(BINA_PHONE) if BINA_PHONE else "not set"
    rec = U.get_user(uid) or {}
    extra = ""
    if uid in ADMIN_IDS:
        extra = "\n\n🛠 You are an <b>admin</b> — use the Admin button."
    elif rec.get("status") != "active":
        extra = ("\n\n⏳ Your account is <b>pending</b> approval. An admin must "
                 "activate you before you can use the bot.")
    await msg.answer(
        "👋 <b>MaklerAssistant</b>\n\n"
        f"Configured number: <b>{who}</b>\n"
        f"Status: <b>{rec.get('status','?')}</b> · Tier: <b>{rec.get('tier','free')}</b>"
        + extra,
        reply_markup=main_menu(uid),
    )


@dp.message(Command("debug"))
async def cmd_debug(msg: Message):
    """Send the newest debug snapshots (screenshot + HTML) to this chat."""
    # Anchor to the script's folder so it works regardless of cwd.
    debug_dir = Path(__file__).resolve().parent / "debug"
    if not debug_dir.exists():
        await msg.answer(f"No debug/ folder yet at <code>{debug_dir}</code> — "
                         "nothing has failed, or snapshots go elsewhere.")
        return
    files = sorted(debug_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        await msg.answer(f"<code>{debug_dir}</code> is empty.")
        return
    await msg.answer(f"Found {len(files)} file(s) in debug/. Sending newest…")
    sent = 0
    for f in files[:6]:
        try:
            await msg.answer_document(FSInputFile(str(f)), caption=f.name)
            sent += 1
        except Exception as exc:
            await msg.answer(f"couldn't send {f.name}: {exc}")
    await msg.answer(f"Sent {sent} file(s). The <code>*-open.html</code> ones "
                     "show opened dropdowns — forward those to Claude.")


@dp.message(Command("cancel"))
async def cancel(msg: Message, state: FSMContext):
    fut = _otp_waiters.get(msg.chat.id)
    if fut and not fut.done():
        fut.cancel()
    ask.cancel(msg.chat.id)
    await state.clear()
    await msg.answer("Cancelled.", reply_markup=main_menu())


# ---- wizard input routing (only fires while a wizard awaits input) --------
@dp.callback_query(F.data.startswith("wz:"))
async def wizard_button(call: CallbackQuery):
    value = call.data[3:]
    await call.answer()
    if value == "__cancel__":
        ask.cancel(call.message.chat.id)
    elif value == "__photos_done__":
        ask.feed_photos_done(call.message.chat.id)
    else:
        ask.feed_choice(call.message.chat.id, value)


@dp.message(F.photo)
async def wizard_photo(msg: Message, bot: Bot):
    if ask.waiting_kind(msg.chat.id) != "photos":
        return
    # download the largest size to a temp file, hand the path to the broker
    import tempfile
    photo = msg.photo[-1]
    path = str(Path(tempfile.gettempdir()) / f"binaphoto_{photo.file_unique_id}.jpg")
    try:
        await bot.download(photo, destination=path)
        ask.feed_photo(msg.chat.id, path)
        await msg.answer("📷 got it — send more, or tap ✅ Done")
    except Exception as exc:
        await msg.answer(f"couldn't save that photo: {exc}")


MENU_TEXTS = {BTN_LOGIN, BTN_NEW, BTN_ADS, BTN_STATUS, BTN_SESSIONS, BTN_ADMIN,
              BTN_S_NEW, BTN_S_SWITCH, BTN_S_LIST, BTN_S_FORGET,
              BTN_A_PENDING, BTN_A_USERS, BTN_A_SETSTATUS, BTN_A_SETTIER, BTN_A_BACK}


# wizard_text is registered later (after the menu-button handlers) so those
# exact-match handlers win for button taps. See _register_wizard_text below.


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
    phone = msg.text.strip()
    await state.clear()
    # record the number under this user (respects tier cap)
    ok, note = U.add_number(msg.from_user.id, phone)
    if not ok:
        await msg.answer(f"⚠️ {note}", reply_markup=main_menu(msg.from_user.id))
        return
    _active_number[msg.from_user.id] = phone   # this number is now active
    await run_login(bot, msg.chat.id, msg.from_user.id, phone, state)


# ---- reply-keyboard taps (buttons in the keyboard area send text) ----
@dp.message(F.text == BTN_LOGIN)
async def kb_login(msg: Message, state: FSMContext, bot: Bot):
    phone = phone_for(None)
    if not phone:
        await state.set_state(Flow.ask_phone)
        await msg.answer("📱 Send your bina.az number (e.g. <code>0557778899</code>):")
        return
    await run_login(bot, msg.chat.id, msg.from_user.id, phone, state)


# ==================== SESSIONS SUBMENU (#2) ====================
@dp.message(F.text == BTN_SESSIONS)
async def kb_sessions(msg: Message):
    await msg.answer("📱 <b>Sessions</b> — manage your connected numbers.",
                     reply_markup=sessions_menu())


@dp.message(F.text == BTN_S_LIST)
async def kb_s_list(msg: Message):
    uid = msg.from_user.id
    nums = U.numbers(uid)
    act = active_phone(uid)
    lines = [f"<b>Your numbers</b> (tier {U.tier_of(uid)}, "
             f"max {U.max_numbers(uid)})", ""]
    if not nums and not act:
        lines.append("None yet — use ➕ New session.")
    for n in (nums or ([act] if act else [])):
        star = " ⭐ active" if act and n.lstrip('+').endswith(mask(act)[-2:]) else ""
        # show masked; mark active
        is_act = (active_phone(uid) or "").lstrip('+')[-9:] == n.lstrip('+')[-9:]
        lines.append(f"• {mask(n)}{' ⭐ active' if is_act else ''}")
    await msg.answer("\n".join(lines), reply_markup=sessions_menu())


@dp.message(F.text == BTN_S_NEW)
async def kb_s_new(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if U.get_user(uid) and U.get_user(uid)["status"] != "active" and uid not in ADMIN_IDS:
        await msg.answer("⏳ Your account isn't active yet.", reply_markup=sessions_menu())
        return
    cap = U.max_numbers(uid); have = len(U.numbers(uid))
    if have >= cap:
        await msg.answer(f"Your tier ({U.tier_of(uid)}) allows {cap} number(s); "
                         f"you have {have}. Ask an admin to upgrade.",
                         reply_markup=sessions_menu())
        return
    await state.set_state(Flow.ask_phone)
    await msg.answer("➕ Send the bina.az number to connect "
                     "(e.g. <code>0701112233</code>). I'll send you the SMS step next.")


@dp.message(F.text == BTN_S_SWITCH)
async def kb_s_switch(msg: Message, bot: Bot, state: FSMContext):
    uid = msg.from_user.id
    nums = U.numbers(uid)
    if len(nums) < 2:
        await msg.answer("You only have one number. Add another with ➕ New session.",
                         reply_markup=sessions_menu())
        return
    opts = [(mask(n), n) for n in nums]
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Busy — finish the current action first.")
        return
    async with lock_for(chat_id):
        try:
            chosen = await ask.ask_choice(bot, chat_id, "Switch active number to:", opts)
            _active_number[uid] = chosen
            await bot.send_message(chat_id, f"🔀 Active number is now {mask(chosen)}.",
                                   reply_markup=sessions_menu())
        except Cancelled:
            await bot.send_message(chat_id, "Cancelled.", reply_markup=sessions_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Timed out.", reply_markup=sessions_menu())


@dp.message(F.text == BTN_S_FORGET)
async def kb_s_forget(msg: Message):
    uid = msg.from_user.id
    phone = active_phone(uid)
    if phone:
        sess = get_session(uid, phone)
        sess.forget()
        await sess.close()
    await msg.answer(f"🚪 Session for {mask(phone) if phone else 'this number'} "
                     "cleared. Next use needs an SMS code.",
                     reply_markup=sessions_menu())
# ==================== END SESSIONS SUBMENU ====================


# ==================== ADMIN PANEL (#5, #6, #7) ====================
def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


@dp.message(F.text == BTN_ADMIN)
async def kb_admin(msg: Message):
    if not _is_admin(msg.from_user.id):
        await msg.answer("⛔️ Admins only.")
        return
    # #7: main buttons -> tap Admin -> admin buttons + Back appear
    await msg.answer("🛠 <b>Admin panel</b>\nManage users, statuses and tiers.",
                     reply_markup=admin_menu())


@dp.message(F.text == BTN_A_BACK)
async def kb_admin_back(msg: Message):
    # #7: Back -> main buttons return
    await msg.answer("Back to the main menu.", reply_markup=main_menu(msg.from_user.id))


@dp.message(F.text == BTN_A_PENDING)
async def kb_admin_pending(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    data = U.all_users()
    pend = {u: r for u, r in data.items() if r.get("status") == "pending"}
    if not pend:
        await msg.answer("No pending users.", reply_markup=admin_menu())
        return
    lines = ["<b>Pending users</b>", ""]
    for u, r in pend.items():
        lines.append(f"• <code>{u}</code> — tier {r.get('tier')}, "
                     f"{len(r.get('numbers', []))} number(s)")
    lines.append("\nUse <b>Set status</b> to activate them.")
    await msg.answer("\n".join(lines), reply_markup=admin_menu())


@dp.message(F.text == BTN_A_USERS)
async def kb_admin_users(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    data = U.all_users()
    if not data:
        await msg.answer("No users yet.", reply_markup=admin_menu())
        return
    lines = ["<b>All users</b>", ""]
    for u, r in data.items():
        star = " 🛠" if int(u) in ADMIN_IDS else ""
        lines.append(f"• <code>{u}</code>{star} — {r.get('status')} / "
                     f"{r.get('tier')} / {len(r.get('numbers', []))} num")
    await msg.answer("\n".join(lines)[:3800], reply_markup=admin_menu())


@dp.message(F.text == BTN_A_SETSTATUS)
async def kb_admin_setstatus(msg: Message, bot: Bot):
    if not _is_admin(msg.from_user.id):
        return
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Busy — finish the current action first.")
        return
    async with lock_for(chat_id):
        try:
            target = await ask.ask_text(bot, chat_id,
                "Send the <b>Telegram user id</b> to change status for:")
            target = target.strip()
            if not target.isdigit():
                await bot.send_message(chat_id, "That's not a numeric id.", reply_markup=admin_menu())
                return
            status = await ask.ask_choice(bot, chat_id,
                f"New status for <code>{target}</code>?",
                [("✅ active", "active"), ("⏳ pending", "pending"), ("🚫 blocked", "blocked")])
            U.ensure_user(int(target))
            U.set_status(int(target), status)
            await bot.send_message(chat_id, f"✅ User <code>{target}</code> → {status}.",
                                   reply_markup=admin_menu())
            # notify the user
            try:
                await bot.send_message(int(target),
                    f"ℹ️ An admin set your status to <b>{status}</b>.")
            except Exception:
                pass
        except Cancelled:
            await bot.send_message(chat_id, "Cancelled.", reply_markup=admin_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Timed out.", reply_markup=admin_menu())


@dp.message(F.text == BTN_A_SETTIER)
async def kb_admin_settier(msg: Message, bot: Bot):
    if not _is_admin(msg.from_user.id):
        return
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Busy — finish the current action first.")
        return
    async with lock_for(chat_id):
        try:
            target = await ask.ask_text(bot, chat_id,
                "Send the <b>Telegram user id</b> to change tier for:")
            target = target.strip()
            if not target.isdigit():
                await bot.send_message(chat_id, "That's not a numeric id.", reply_markup=admin_menu())
                return
            tier = await ask.ask_choice(bot, chat_id,
                f"New tier for <code>{target}</code>?",
                [("Free (1 num)", "free"), ("Pro (2 num)", "pro"), ("Diamond (5 num)", "diamond")])
            U.ensure_user(int(target))
            U.set_tier(int(target), tier)
            await bot.send_message(chat_id, f"⭐ User <code>{target}</code> → {tier}.",
                                   reply_markup=admin_menu())
            try:
                await bot.send_message(int(target),
                    f"⭐ An admin upgraded your tier to <b>{tier}</b>.")
            except Exception:
                pass
        except Cancelled:
            await bot.send_message(chat_id, "Cancelled.", reply_markup=admin_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Timed out.", reply_markup=admin_menu())
# ==================== END ADMIN PANEL ====================


@dp.message(F.text == BTN_STATUS)
async def kb_status(msg: Message):
    phone = phone_for(None)
    if not phone:
        await msg.answer("No number configured yet.", reply_markup=main_menu())
        return
    owner = security.owner_of(phone)
    if owner is not None and str(owner) != str(msg.from_user.id):
        await msg.answer("🔒 That number belongs to another user.", reply_markup=main_menu())
        return
    sess = get_session(msg.from_user.id, phone)
    saved = sess.session_file.exists()
    enc = "on" if security.encryption_enabled() else "off"
    await msg.answer(
        f"<b>{mask(phone)}</b>\n"
        f"Saved session: {'🟢 yes' if saved else '⚪️ none (will need SMS)'}\n"
        f"Encryption: {enc} · owned by you: {'yes' if owner else 'unclaimed'}",
        reply_markup=main_menu())


@dp.message(F.text == BTN_ADS)
async def kb_ads(msg: Message, bot: Bot, state: FSMContext):
    phone = phone_for(None)
    if not phone:
        await msg.answer("Configure a number first (🔑 Login).")
        return
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Busy — one action at a time.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, msg.from_user.id, phone, state)
        if not ok:
            return
        sess = get_session(msg.from_user.id, phone)
        await bot.send_message(chat_id, "📋 Fetching your ads…")
        try:
            async with sess.lock:
                ads = await PublishFlow(sess).fetch_my_ads()
        except Exception as exc:
            log.exception("fetch my ads")
            await bot.send_message(chat_id, f"❌ Couldn't read your ads: {exc}",
                                   reply_markup=main_menu())
            return
    if not ads:
        await bot.send_message(chat_id, "You have no ads on this account yet.",
                               reply_markup=main_menu())
        return
    lines = [f"📋 <b>Your ads ({len(ads)})</b> — {mask(phone)}", ""]
    for a in ads:
        line = f"🏠 <b>{a.get('title') or 'Ad'}</b>"
        if a.get("price"):
            line += f" — {a['price']} ₼"
        lines.append(line)
        if a.get("params"):
            lines.append(f"   {a['params']}")
        meta = []
        if a.get("id"):
            meta.append(f"id {a['id']}")
        if a.get("status"):
            meta.append(a["status"])
        if meta:
            lines.append("   " + " · ".join(meta))
        lines.append("")
    # chunk to stay under Telegram's 4096 limit
    buf = ""
    for ln in lines:
        if len(buf) + len(ln) > 3500:
            await bot.send_message(chat_id, buf)
            buf = ""
        buf += ln + "\n"
    await bot.send_message(chat_id, buf or "—", reply_markup=main_menu())


@dp.message(F.text == BTN_NEW)
async def kb_new(msg: Message, bot: Bot, state: FSMContext):
    phone = phone_for(None)
    if not phone:
        await msg.answer("Configure a number first (🔑 Login).")
        return
    if lock_for(msg.chat.id).locked():
        await msg.answer("⏳ Busy — one action at a time.")
        return
    async with lock_for(msg.chat.id):
        ok = await ensure_login(bot, msg.chat.id, msg.from_user.id, phone, state)
        if not ok:
            return
        sess = get_session(msg.from_user.id, phone)
        try:
            await publish_wizard(bot, msg.chat.id, sess)
        except Cancelled:
            await bot.send_message(msg.chat.id, "🛑 Publishing cancelled.", reply_markup=main_menu())
        except PublishError as exc:
            await bot.send_message(msg.chat.id, f"❌ {exc}", reply_markup=main_menu())
        except asyncio.TimeoutError:
            await bot.send_message(msg.chat.id, "⏰ Timed out waiting for input.", reply_markup=main_menu())
        except Exception as exc:
            log.exception("publish wizard error")
            await bot.send_message(msg.chat.id, f"💥 {exc}", reply_markup=main_menu())


@dp.message(F.text)
async def wizard_text(msg: Message, state: FSMContext):
    """Catch-all for typed answers the wizard is awaiting. Registered AFTER the
    menu-button handlers so exact-match button taps win."""
    if msg.text in MENU_TEXTS:
        return
    if await state.get_state() is not None:
        return
    if ask.waiting_kind(msg.chat.id) in ("text", "choice"):
        ask.feed_text(msg.chat.id, msg.text)


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
    user_id = call.from_user.id
    owner = security.owner_of(phone)
    if owner is not None and str(owner) != str(user_id):
        await call.message.answer("🔒 That number belongs to another user.",
                                  reply_markup=main_menu())
        return
    sess = get_session(user_id, phone)
    saved = sess.session_file.exists()
    enc = "on" if security.encryption_enabled() else "off"
    await call.message.answer(
        f"<b>{mask(phone)}</b>\n"
        f"Saved session: {'🟢 yes' if saved else '⚪️ none (will need SMS)'}\n"
        f"Encryption: {enc} · owned by you: {'yes' if owner else 'unclaimed'}",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "logout")
async def cb_logout(call: CallbackQuery):
    await call.answer("Session forgotten")
    phone = phone_for(None)
    user_id = call.from_user.id
    if phone and (security.owner_of(phone) in (None, user_id) or
                  str(security.owner_of(phone)) == str(user_id)):
        sess = get_session(user_id, phone)
        sess.forget()
        await sess.close()
    await call.message.answer(
        "🚪 Saved session cleared. Next login needs an SMS code.\n"
        "<i>(You still own this number — it stays reserved to you.)</i>",
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
        ok = await ensure_login(bot, chat_id, call.from_user.id, phone, state)
        if not ok:
            return
        await call.message.answer("📋 You're logged in. (Ad-listing readout is the "
                                  "next feature to add — the session is ready for it.)",
                                  reply_markup=main_menu())


PUBLISHER_NAME = os.getenv("PUBLISHER_NAME", "").strip()
PUBLISHER_EMAIL = os.getenv("PUBLISHER_EMAIL", "").strip()


@dp.callback_query(F.data == "newlisting")
async def cb_newlisting(call: CallbackQuery, bot: Bot, state: FSMContext):
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
        ok = await ensure_login(bot, chat_id, call.from_user.id, phone, state)
        if not ok:
            return
        sess = get_session(call.from_user.id, phone)
        try:
            await publish_wizard(bot, chat_id, sess)
        except Cancelled:
            await bot.send_message(chat_id, "🛑 Publishing cancelled.", reply_markup=main_menu())
        except PublishError as exc:
            await bot.send_message(chat_id, f"❌ {exc}\nA debug snapshot was saved.",
                                   reply_markup=main_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Timed out waiting for input.", reply_markup=main_menu())
        except Exception as exc:
            log.exception("publish wizard error")
            await bot.send_message(chat_id, f"💥 {exc}", reply_markup=main_menu())


async def _choose_from_dropdown(bot, chat_id, flow, opener_key, prompt,
                                filter_text=None, tag="dropdown", optional=False,
                                known=None):
    """Open a bina.az dropdown, show its options as buttons, click the choice.

    If live discovery finds nothing but a `known` option list is supplied, we
    show those instead and click by visible text. Long lists are capped to 90.
    If nothing is found and optional=True, we keep the field's current value.
    """
    options = await flow.discover_options(opener_key, filter_text=filter_text, tag=tag)
    if not options and known:
        await bot.send_message(chat_id, f"ℹ️ Using the known {tag} list.")
        options = list(known)
    if not options:
        if optional:
            await bot.send_message(
                chat_id, f"ℹ️ Couldn't read the {tag} options — keeping the "
                         f"field's current value. (Use /debug to inspect.)")
            return None
        raise PublishError(
            f"Opened the {tag} dropdown but found no options. Run /debug and "
            f"send me the *-{tag}-open.html to pin it.")
    note = ""
    if len(options) > 90:
        options = options[:90]
        note = "\n<i>(first 90 shown)</i>"
    labeled = [(opt[:40], str(i)) for i, opt in enumerate(options)]
    chosen_idx = await ask.ask_choice(bot, chat_id, prompt + note, labeled)
    chosen_text = options[int(chosen_idx)]
    try:
        await flow.pick_option(chosen_text, tag=tag)
    except PublishError:
        # Clicking by text failed (option overlay differs) — tell the user but
        # continue; the field may already hold an acceptable default.
        if not optional:
            raise
        await bot.send_message(chat_id, f"⚠️ Couldn't click '{chosen_text}' — "
                                        f"keeping the current {tag} value.")
    return chosen_text


async def _search_pick(bot, chat_id, flow, opener_key, label, tag, optional=False):
    """Ask the user to type a search term, then show matching results as buttons.

    bina.az city/district/village fields are search-dropdowns. The user types
    e.g. 'Nizami', we read the filtered results and show them (5 at a time with
    a 'Digər' more button).
    """
    query = await ask.ask_text(
        bot, chat_id,
        f"{label} — type a few letters to search (or send <b>-</b> to skip)."
        if optional else
        f"{label} — type a few letters to search:")
    if optional and query.strip() in ("-", "skip", "Skip"):
        return None

    results = await flow.search_and_pick(opener_key, query.strip(), tag=tag)
    if not results:
        if optional:
            await bot.send_message(chat_id, f"No {tag} matches — skipping.")
            return None
        # let them retry once with a broader term
        results = await flow.search_and_pick(opener_key, query.strip()[:2], tag=tag)
        if not results:
            raise PublishError(f"No {tag} results for '{query}'. Run /debug and "
                               f"send me *-{tag}-open.html.")

    page = 0
    while True:
        chunk = results[page * 5:(page + 1) * 5]
        opts = [(r[:40], f"r{page*5+i}") for i, r in enumerate(chunk)]
        if (page + 1) * 5 < len(results):
            opts.append(("➡️ Digər (more)", "more"))
        opts.append(("🔁 Search again", "again"))
        chosen = await ask.ask_choice(bot, chat_id, f"{label}: pick one", opts)
        if chosen == "more":
            page += 1
            continue
        if chosen == "again":
            return await _search_pick(bot, chat_id, flow, opener_key, label, tag, optional)
        pick = results[int(chosen[1:])]
        try:
            await flow.pick_result(pick, tag=tag)
        except PublishError:
            await bot.send_message(chat_id, f"⚠️ Couldn't select {pick}.")
        return pick


async def publish_wizard(bot: Bot, chat_id: int, sess: BinaSession):
    flow = PublishFlow(sess)
    await bot.send_message(chat_id, "🏗 <b>New listing</b> — let's go. I'll ask one thing at a time.")

    async with sess.lock:
        await flow.open_new_ad()

        # Deal type is always SELL (this bot is for selling) — set silently.
        await flow.choose_deal(sell=True)

        # Category = property type. Asked ONCE here; sets the type dropdown.
        cat = await ask.ask_choice(bot, chat_id, "Property type?",
                                   [("Yeni tikili", "Yeni tikili"),
                                    ("Köhnə tikili", "Köhnə tikili")])
        await flow.choose_category(cat)

        # Owner vs agent
        who = await ask.ask_choice(bot, chat_id, "You are the…",
                                   [("Owner (Elanın sahibi)", "owner"),
                                    ("Agent (Vasitəçi)", "agent")])
        is_owner = who == "owner"
        await flow.choose_owner(is_owner)

        # City — type-to-search then pick from results (paged 5 at a time).
        city = await _search_pick(bot, chat_id, flow, "city_button", "City (Şəhər)",
                                  tag="city")

        # Rayon (district) exists ONLY for Bakı.
        if (city or "").strip().lower() in ("bakı", "baki", "baku"):
            await _search_pick(bot, chat_id, flow, "district_button",
                               "District (Rayon)", tag="district", optional=True)
            await _search_pick(bot, chat_id, flow, "village_button",
                               "Settlement (Qəsəbə)", tag="village", optional=True)

        address = await ask.ask_text(bot, chat_id,
                                     "Exact address (Ünvan / dəqiq yerləşmə)?")

        # Map: optionally pin the location on the map and confirm the popup (#4).
        want_map = await ask.ask_choice(bot, chat_id,
                                        "Set the location on the map?",
                                        [("📍 Yes", "yes"), ("Skip", "no")])
        if want_map == "yes":
            ok = await flow.open_map_and_confirm()
            await bot.send_message(chat_id,
                "📍 Map location confirmed." if ok else
                "⚠️ Couldn't auto-confirm the map — set it manually later if needed.")

        rooms = await ask.ask_text(bot, chat_id, "Number of rooms (Otaq sayı)?")
        area = await ask.ask_text(bot, chat_id, "Area in m² (Sahə)?")
        floor = await ask.ask_text(bot, chat_id, "Floor (Mərtəbə)?")
        total = await ask.ask_text(bot, chat_id, "Total floors (Mərtəbələrin sayı)?")

        repair = await ask.ask_choice(bot, chat_id, "Repair (Təmir)?",
                                      [("Təmirli (yes)", "yes"), ("Təmirsiz (no)", "no")])
        await flow.set_repair(repair == "yes")

        desc = await ask.ask_text(bot, chat_id,
                                  "Description (Əlavə məlumat). Don't include phone/email.")
        price = await ask.ask_text(bot, chat_id, "Price (Qiymət) in AZN?")

        await flow.fill_details(address=address, rooms=rooms, area=area, floor=floor,
                                total_floors=total, description=desc, price=price)

        # optional checkboxes
        extras = await ask.ask_choice(bot, chat_id, "Any of these apply?",
                                      [("Çıxarış var (bill of sale)", "bill"),
                                       ("İpoteka var (mortgage)", "mortgage"),
                                       ("Neither", "none")])
        if extras == "bill":
            await flow.set_checkbox("bill_of_sale", True)
        elif extras == "mortgage":
            await flow.set_checkbox("mortgage", True)

        # photos (min 4, max 30)
        while True:
            photos = await ask.ask_photos(
                bot, chat_id,
                "📷 Send your photos (at least <b>4</b>, at most <b>30</b>), "
                "then tap ✅ Done.\n"
                "<i>No screenshots, logos, framed or blurry photos.</i>")
            if len(photos) < 4:
                await bot.send_message(chat_id,
                    f"Only {len(photos)} photo(s). bina.az needs at least 4 — "
                    "send more.")
                continue
            if len(photos) > 30:
                photos = photos[:30]
                await bot.send_message(chat_id, "Using the first 30 photos.")
            break
        await flow.add_photos(photos)

        # contact
        name = PUBLISHER_NAME or await ask.ask_text(bot, chat_id, "Your name (Ad)?")
        email = PUBLISHER_EMAIL or await ask.ask_text(bot, chat_id, "Your e-mail?")
        await flow.fill_contact(name=name, email=email, is_owner=is_owner)

        # review + submit
        summary = (f"<b>Review</b>\n"
                   f"• {cat} · Sell\n"
                   f"• {city} · {rooms} rooms · {area} m² · floor {floor}/{total}\n"
                   f"• Repair: {repair} · Price: {price} AZN\n"
                   f"• {len(photos)} photos\n\n"
                   f"Tap Continue to submit (bina.az may then show a package step).")
        go = await ask.ask_choice(bot, chat_id, summary,
                                  [("▶️ Continue (Davam etmək)", "go")])
        if go != "go":
            raise Cancelled()

        final_url = await flow.submit()

    await bot.send_message(
        chat_id,
        "✅ <b>Listing submitted!</b>\n\n"
        "Your ad has been sent to bina.az for review. Once their moderators "
        "approve it, it goes live on the site.\n\n"
        "Check status anytime with 📋 My ads.",
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


async def ensure_login(bot: Bot, chat_id: int, user_id: int, phone: str,
                       state: FSMContext, announce_reused: bool = False) -> bool:
    # Ownership gate: a number belongs to the first user who connected it.
    if not check_ownership(phone, user_id):
        await bot.send_message(
            chat_id,
            "🔒 This number is already connected to a different user account. "
            "For privacy and security, a bina.az number can only be used by the "
            "Telegram account that first connected it.",
            reply_markup=main_menu(),
        )
        return False

    sess = get_session(user_id, phone)
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
    # Only mention a reused session when the user explicitly tapped Login.
    if not fresh and announce_reused:
        await bot.send_message(chat_id, "✅ Already logged in (saved session — no SMS needed).")
    return True


async def run_login(bot: Bot, chat_id: int, user_id: int, phone: str, state: FSMContext):
    if lock_for(chat_id).locked():
        await bot.send_message(chat_id, "⏳ Busy — one action at a time.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, user_id, phone, state, announce_reused=True)
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
