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
    report = State()


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
BTN_S_REMOVE = "🗑 Remove number"
BTN_CONTACT = "✉️ Contact / Report"

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
        [KeyboardButton(text=BTN_CONTACT)],
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
            [KeyboardButton(text=BTN_S_REMOVE)],
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
    uname = msg.from_user.username or ""
    fname = msg.from_user.full_name or ""
    U.ensure_user(uid, default_status="active" if uid in ADMIN_IDS else "pending",
                  username=uname, name=fname)
    if uid in ADMIN_IDS and not U.is_active(uid):
        U.set_status(uid, "active")
    rec = U.get_user(uid) or {}
    tier_az = {"free": "Pulsuz", "pro": "Pro", "diamond": "Diamond"}.get(rec.get("tier","free"), rec.get("tier"))
    status_az = {"active": "Aktiv", "pending": "Gözləmədə", "blocked": "Bloklanıb"}.get(rec.get("status","?"), rec.get("status"))
    extra = ""
    if uid in ADMIN_IDS:
        extra = "\n\n🛠 Siz <b>adminsiniz</b> — Admin düyməsindən istifadə edin."
    elif rec.get("status") != "active":
        extra = ("\n\n⏳ Hesabınız <b>təsdiq gözləyir</b>. Admin sizi aktivləşdirənə "
                 "qədər botdan istifadə edə bilməzsiniz.")
    await msg.answer(
        "👋 <b>MaklerAssistant</b>\n\n"
        f"Status: <b>{status_az}</b> · Tarif: <b>{tier_az}</b>"
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
    await msg.answer("Ləğv edildi.", reply_markup=main_menu())


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
        await msg.answer("📷 alındı — daha göndərin və ya ✅ Bitdi düyməsinə basın")
    except Exception as exc:
        await msg.answer(f"şəkli saxlaya bilmədim: {exc}")


MENU_TEXTS = {BTN_LOGIN, BTN_NEW, BTN_ADS, BTN_STATUS, BTN_SESSIONS, BTN_ADMIN,
              BTN_S_NEW, BTN_S_SWITCH, BTN_S_LIST, BTN_S_FORGET, BTN_S_REMOVE,
              BTN_CONTACT,
              BTN_A_PENDING, BTN_A_USERS, BTN_A_SETSTATUS, BTN_A_SETTIER, BTN_A_BACK}


# wizard_text is registered later (after the menu-button handlers) so those
# exact-match handlers win for button taps. See _register_wizard_text below.


# ---- OTP relay: any digits while we're waiting go to the login coroutine ---
@dp.message(Flow.ask_otp, F.text.regexp(r"^\s*\d[\d\s\-]{2,9}\s*$"))
async def got_otp(msg: Message):
    fut = _otp_waiters.get(msg.chat.id)
    if fut and not fut.done():
        fut.set_result(msg.text)
        # Acknowledge FIRST, then delete the code for privacy.
        await msg.answer("🔑 Kod alındı, təsdiqlənir…")
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
        await msg.answer("📱 bina.az nömrənizi göndərin (məs. <code>0557778899</code>):")
        return
    await run_login(bot, msg.chat.id, msg.from_user.id, phone, state)


# ==================== CONTACT / REPORT ====================
@dp.message(F.text == BTN_CONTACT)
async def kb_contact(msg: Message, state: FSMContext):
    await state.set_state(Flow.report)
    await msg.answer(
        "✉️ <b>Əlaqə / Problem bildir</b>\n\n"
        "Problemi və ya sorğunu bir mesajda yazın — admində çatdıracam. "
        "İstəsəniz <b>şəkil də göndərə bilərsiniz</b>.\n\n"
        "<i>Dayandırmaq üçün /cancel.</i>")


async def _forward_report_to_admins(bot, msg, caption_text):
    uid = msg.from_user.id
    label = U.label_for(uid)
    tier = U.tier_of(uid)
    status = (U.get_user(uid) or {}).get("status", "?")
    header = (f"📩 <b>Yeni müraciət</b>\n"
              f"Kimdən: {label}\n"
              f"Tarif: {tier} · Status: {status}")
    forwarded = 0
    for admin_id in ADMIN_IDS:
        try:
            if msg.photo:
                # forward the photo with the report text as caption
                await bot.send_photo(admin_id, msg.photo[-1].file_id,
                                     caption=f"{header}\n\n{caption_text}"[:1024])
            else:
                await bot.send_message(admin_id, f"{header}\n\n{caption_text}")
            forwarded += 1
        except Exception:
            pass
    return forwarded


@dp.message(Flow.report, F.photo)
async def got_report_photo(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid = msg.from_user.id
    if not ADMIN_IDS:
        await msg.answer("⚠️ Admin təyin olunmayıb.", reply_markup=main_menu(uid))
        return
    text = (msg.caption or "").strip() or "(şəkil, mətn yoxdur)"
    n = await _forward_report_to_admins(bot, msg, text)
    await msg.answer(
        "✅ Adminə göndərildi. Tezliklə cavab veriləcək." if n else
        "⚠️ Admin əlçatan deyil — sonra yenidən cəhd edin.",
        reply_markup=main_menu(uid))


@dp.message(Flow.report)
async def got_report(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    text = (msg.text or "").strip()
    uid = msg.from_user.id
    if not text or text.lower() in ("/cancel", "/stop"):
        await msg.answer("Ləğv edildi.", reply_markup=main_menu(uid))
        return
    if not ADMIN_IDS:
        await msg.answer("⚠️ Admin təyin olunmayıb.", reply_markup=main_menu(uid))
        return
    n = await _forward_report_to_admins(bot, msg, text)
    await msg.answer(
        "✅ Adminə göndərildi. Tezliklə cavab veriləcək." if n else
        "⚠️ Admin əlçatan deyil — sonra yenidən cəhd edin.",
        reply_markup=main_menu(uid))
# ==================== END CONTACT / REPORT ====================


# ==================== SESSIONS SUBMENU (#2) ====================
@dp.message(F.text == BTN_SESSIONS)
async def kb_sessions(msg: Message):
    await msg.answer("📱 <b>Sessiyalar</b> — qoşulmuş nömrələrinizi idarə edin.",
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
        await msg.answer("Yalnız bir nömrəniz var. ➕ Yeni sessiya ilə başqa nömrə əlavə edin.",
                         reply_markup=sessions_menu())
        return
    opts = [(mask(n), n) for n in nums]
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Məşğul — əvvəlki əməliyyatı bitirin.")
        return
    async with lock_for(chat_id):
        try:
            chosen = await ask.ask_choice(bot, chat_id, "Aktiv nömrəni dəyişin:", opts)
            _active_number[uid] = chosen
            await bot.send_message(chat_id, f"🔀 Active number is now {mask(chosen)}.",
                                   reply_markup=sessions_menu())
        except Cancelled:
            await bot.send_message(chat_id, "Ləğv edildi.", reply_markup=sessions_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Vaxt bitdi.", reply_markup=sessions_menu())


@dp.message(F.text == BTN_S_FORGET)
async def kb_s_forget(msg: Message):
    uid = msg.from_user.id
    phone = active_phone(uid)
    if phone:
        sess = get_session(uid, phone)
        sess.forget()
        await sess.close()
    await msg.answer(
        f"🚪 Login session for {mask(phone) if phone else 'this number'} cleared "
        "— the next action will ask for a fresh SMS code.\n\n"
        "<i>The number stays connected to your account. To remove it entirely, "
        "use 🗑 Remove number.</i>",
        reply_markup=sessions_menu())


@dp.message(F.text == BTN_S_REMOVE)
async def kb_s_remove(msg: Message, bot: Bot, state: FSMContext):
    uid = msg.from_user.id
    nums = U.numbers(uid)
    if not nums:
        await msg.answer("Qoşulmuş nömrəniz yoxdur.", reply_markup=sessions_menu())
        return
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Məşğul — əvvəlki əməliyyatı bitirin.")
        return
    async with lock_for(chat_id):
        try:
            opts = [(mask(n), n) for n in nums]
            chosen = await ask.ask_choice(bot, chat_id,
                "Hesabınızdan hansı nömrəni silək?", opts)
            # clear its saved session, drop it from the user's list
            sess = get_session(uid, chosen)
            sess.forget()
            await sess.close()
            U.remove_number(uid, chosen)
            _active_number.pop(uid, None)
            await bot.send_message(chat_id,
                f"🗑 Removed {mask(chosen)} from your account.\n\n"
                "<i>Note: the number stays reserved to your Telegram ID for "
                "privacy. To reassign it to someone else, contact an admin via "
                "✉️ Contact / Report.</i>",
                reply_markup=sessions_menu())
        except Cancelled:
            await bot.send_message(chat_id, "Ləğv edildi.", reply_markup=sessions_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Vaxt bitdi.", reply_markup=sessions_menu())
# ==================== END SESSIONS SUBMENU ====================


# ==================== ADMIN PANEL (#5, #6, #7) ====================
def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


@dp.message(F.text == BTN_ADMIN)
async def kb_admin(msg: Message):
    if not _is_admin(msg.from_user.id):
        await msg.answer("⛔️ Yalnız adminlər üçün.")
        return
    # #7: main buttons -> tap Admin -> admin buttons + Back appear
    await msg.answer("🛠 <b>Admin panel</b>\nManage users, statuses and tiers.",
                     reply_markup=admin_menu())


@dp.message(F.text == BTN_A_BACK)
async def kb_admin_back(msg: Message):
    await msg.answer("Əsas menyuya qayıdıldı.", reply_markup=main_menu(msg.from_user.id))


def _user_line(uid_str, r):
    """One clickable line: '/id @username (Name) — status / tier / N num'."""
    label = U.label_for(int(uid_str))
    star = " 🛠" if int(uid_str) in ADMIN_IDS else ""
    return (f"{label}{star}\n   {r.get('status')} · {r.get('tier')} · "
            f"{len(r.get('numbers', []))} nömrə")


@dp.message(F.text == BTN_A_PENDING)
async def kb_admin_pending(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    pend = {u: r for u, r in U.all_users().items() if r.get("status") == "pending"}
    if not pend:
        await msg.answer("Gözləyən istifadəçi yoxdur.", reply_markup=admin_menu())
        return
    lines = ["⏳ <b>Gözləyən istifadəçilər</b>",
             "Statusu dəyişmək üçün ID-yə toxunun:", ""]
    for u, r in pend.items():
        lines.append(_user_line(u, r))
    await msg.answer("\n".join(lines)[:3800], reply_markup=admin_menu())


@dp.message(F.text == BTN_A_USERS)
async def kb_admin_users(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    data = U.all_users()
    if not data:
        await msg.answer("Hələ istifadəçi yoxdur.", reply_markup=admin_menu())
        return
    lines = ["👥 <b>Bütün istifadəçilər</b>",
             "İdarə etmək üçün ID-yə toxunun:", ""]
    for u, r in data.items():
        lines.append(_user_line(u, r))
    await msg.answer("\n".join(lines)[:3800], reply_markup=admin_menu())


@dp.message(F.text == BTN_A_SETSTATUS)
async def kb_admin_setstatus(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    await kb_admin_users(msg)   # show the clickable list; tapping an ID opens actions


@dp.message(F.text == BTN_A_SETTIER)
async def kb_admin_settier(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    await kb_admin_users(msg)


# Tapping a "/123456" line opens an action menu for that user (admin only).
@dp.message(F.text.regexp(r"^/\d{4,}$"))
async def admin_pick_user(msg: Message, bot: Bot):
    if not _is_admin(msg.from_user.id):
        return
    target = int(msg.text.strip().lstrip("/"))
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Məşğul — əvvəlki əməliyyatı bitirin.")
        return
    async with lock_for(chat_id):
        try:
            U.ensure_user(target)
            action = await ask.ask_choice(bot, chat_id,
                f"{U.label_for(target)} — nə edək?",
                [("✅ Aktiv et", "st:active"), ("⏳ Gözləmə", "st:pending"),
                 ("🚫 Blokla", "st:blocked"),
                 ("⭐ Tarif: Pulsuz", "ti:free"), ("⭐ Tarif: Pro", "ti:pro"),
                 ("⭐ Tarif: Diamond", "ti:diamond")])
            kind, val = action.split(":")
            if kind == "st":
                U.set_status(target, val)
                az = {"active":"Aktiv","pending":"Gözləmədə","blocked":"Bloklandı"}[val]
                await bot.send_message(chat_id, f"✅ {U.label_for(target)} → {az}",
                                       reply_markup=admin_menu())
                try:
                    await bot.send_message(target, f"ℹ️ Admin statusunuzu dəyişdi: <b>{az}</b>.")
                except Exception:
                    pass
            else:
                U.set_tier(target, val)
                await bot.send_message(chat_id, f"⭐ {U.label_for(target)} → {val}",
                                       reply_markup=admin_menu())
                try:
                    await bot.send_message(target, f"⭐ Tarifiniz dəyişdirildi: <b>{val}</b>.")
                except Exception:
                    pass
        except Cancelled:
            await bot.send_message(chat_id, "Ləğv edildi.", reply_markup=admin_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Vaxt bitdi.", reply_markup=admin_menu())
# ==================== END ADMIN PANEL ====================


@dp.message(F.text == BTN_STATUS)
async def kb_status(msg: Message):
    phone = phone_for(None)
    if not phone:
        await msg.answer("Hələ nömrə təyin olunmayıb.", reply_markup=main_menu())
        return
    owner = security.owner_of(phone)
    if owner is not None and str(owner) != str(msg.from_user.id):
        await msg.answer("🔒 Bu nömrə başqa istifadəçiyə aiddir.", reply_markup=main_menu())
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
        await msg.answer("Əvvəlcə nömrə əlavə edin (🔑 Giriş).")
        return
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Məşğul — eyni anda bir əməliyyat.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, msg.from_user.id, phone, state)
        if not ok:
            return
        sess = get_session(msg.from_user.id, phone)
        await bot.send_message(chat_id, "📋 Elanlarınız yüklənir…")
        try:
            async with sess.lock:
                ads = await PublishFlow(sess).fetch_my_ads()
        except Exception as exc:
            log.exception("fetch my ads")
            await bot.send_message(chat_id, f"❌ Couldn't read your ads: {exc}",
                                   reply_markup=main_menu())
            return
    if not ads:
        await bot.send_message(chat_id, "Bu hesabda hələ elan yoxdur.",
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
        await msg.answer("Əvvəlcə nömrə əlavə edin (🔑 Giriş).")
        return
    if lock_for(msg.chat.id).locked():
        await msg.answer("⏳ Məşğul — eyni anda bir əməliyyat.")
        return
    async with lock_for(msg.chat.id):
        ok = await ensure_login(bot, msg.chat.id, msg.from_user.id, phone, state)
        if not ok:
            return
        sess = get_session(msg.from_user.id, phone)
        try:
            await publish_wizard(bot, msg.chat.id, sess)
        except Cancelled:
            await bot.send_message(msg.chat.id, "🛑 Elan yerləşdirmə ləğv edildi.", reply_markup=main_menu())
        except PublishError as exc:
            await bot.send_message(msg.chat.id, f"❌ {exc}", reply_markup=main_menu())
        except asyncio.TimeoutError:
            await bot.send_message(msg.chat.id, "⏰ Cavab gözlənilərkən vaxt bitdi.", reply_markup=main_menu())
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
        await call.message.answer("Hələ nömrə təyin olunmayıb.", reply_markup=main_menu())
        return
    user_id = call.from_user.id
    owner = security.owner_of(phone)
    if owner is not None and str(owner) != str(user_id):
        await call.message.answer("🔒 Bu nömrə başqa istifadəçiyə aiddir.",
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
        await call.message.answer("Əvvəlcə nömrə əlavə edin (🔑 Giriş).")
        return
    chat_id = call.message.chat.id
    if lock_for(chat_id).locked():
        await call.message.answer("⏳ Məşğul — eyni anda bir əməliyyat.")
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

# Known lists from cities.txt — used to build the choice buttons so the user
# always sees correct names, independent of live-scrape quirks.
CITIES = [
    "Ağcabədi","Ağdam","Ağdaş","Ağdərə","Ağstafa","Ağsu","Astara","Bakı","Balakən",
    "Beyləqan","Bərdə","Biləsuvar","Cəbrayıl","Cəlilabad","Daşkəsən","Füzuli",
    "Gədəbəy","Gəncə","Goranboy","Göyçay","Göygöl","Göytəpə","Hacıqabul","Xaçmaz",
    "Xankəndi","Xırdalan","Xızı","Xocalı","Xocavənd","Xudat","İmişli","İsmayıllı",
    "Kəlbəcər","Kürdəmir","Qax","Qazax","Qəbələ","Qobustan","Quba","Qubadlı","Qusar",
    "Laçın","Lerik","Lənkəran","Masallı","Mingəçevir","Naftalan","Naxçıvan",
    "Naxçıvan MR","Neftçala","Oğuz","Saatlı","Sabirabad","Salyan","Samux","Siyəzən",
    "Sumqayıt","Şabran","Şamaxı","Şəki","Şəmkir","Şirvan","Şuşa","Tərtər","Tovuz",
    "Ucar","Yardımlı","Yevlax","Zaqatala","Zəngilan","Zərdab",
]
BAKU_DISTRICTS = [
    "Abşeron","Binəqədi","Xətai","Xəzər","Qaradağ","Nərimanov","Nəsimi","Nizami",
    "Pirallahı","Sabunçu","Səbail","Suraxanı","Yasamal",
]


@dp.callback_query(F.data == "newlisting")
async def cb_newlisting(call: CallbackQuery, bot: Bot, state: FSMContext):
    await call.answer()
    phone = phone_for(None)
    if not phone:
        await call.message.answer("Əvvəlcə nömrə əlavə edin (🔑 Giriş).")
        return
    chat_id = call.message.chat.id
    if lock_for(chat_id).locked():
        await call.message.answer("⏳ Məşğul — eyni anda bir əməliyyat.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, call.from_user.id, phone, state)
        if not ok:
            return
        sess = get_session(call.from_user.id, phone)
        try:
            await publish_wizard(bot, chat_id, sess)
        except Cancelled:
            await bot.send_message(chat_id, "🛑 Elan yerləşdirmə ləğv edildi.", reply_markup=main_menu())
        except PublishError as exc:
            await bot.send_message(chat_id, f"❌ {exc}\nA debug snapshot was saved.",
                                   reply_markup=main_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Cavab gözlənilərkən vaxt bitdi.", reply_markup=main_menu())
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


async def _pick_list(bot, chat_id, flow, opener_key, label, tag,
                     known=None, optional=False):
    """Show the option buttons (from a known list), then select on the page.

    The choice buttons come from `known` (guaranteed-correct names). Once the
    user picks, we open the dropdown, TYPE that name into its search box, and
    click the matching radio row — which is robust and avoids relying on
    scraping the full list. Falls back to live scraping if no known list.
    """
    options = list(known) if known else await flow.search_and_pick(opener_key, "", tag=tag)
    if not options:
        if optional:
            await bot.send_message(chat_id, f"No {tag} options — skipping.")
            return None
        raise PublishError(f"No {tag} options. Run /debug, send *-{tag}-open.html.")

    page = 0
    while True:
        chunk = options[page * 5:(page + 1) * 5]
        opts = [(r[:40], f"r{page*5+i}") for i, r in enumerate(chunk)]
        if (page + 1) * 5 < len(options):
            opts.append(("➡️ Digər", "more"))
        chosen = await ask.ask_choice(bot, chat_id, f"{label}:", opts)
        if chosen == "more":
            page += 1
            continue
        pick = options[int(chosen[1:])]
        break

    # Now select it on the page: open dropdown, type the name, click the row.
    try:
        results = await flow.search_and_pick(opener_key, pick, tag=tag)
        # click exact match if present, else the first result
        target = pick if pick in results else (results[0] if results else None)
        if target:
            await flow.pick_result(target, tag=tag)
        elif not optional:
            await bot.send_message(chat_id,
                f"⚠️ Typed '{pick}' but saw no match on the page — continuing.")
    except PublishError:
        if not optional:
            await bot.send_message(chat_id, f"⚠️ Couldn't select {pick} on the page.")
    return pick


async def publish_wizard(bot: Bot, chat_id: int, sess: BinaSession):
    flow = PublishFlow(sess)
    await bot.send_message(chat_id, "🏗 <b>Yeni elan</b> — başlayaq. Hər dəfə bir sual verəcəm.")

    async with sess.lock:
        await flow.open_new_ad()

        # Deal type is always SELL (this bot is for selling) — set silently.
        await flow.choose_deal(sell=True)

        # Category = property type. Asked ONCE here; sets the type dropdown.
        cat = await ask.ask_choice(bot, chat_id, "Əmlakın növü?",
                                   [("Yeni tikili", "Yeni tikili"),
                                    ("Köhnə tikili", "Köhnə tikili")])
        await flow.choose_category(cat)

        # Owner vs agent — HIDDEN for now, defaults to Agent.
        # To re-enable later, uncomment the ask_choice block below.
        # who = await ask.ask_choice(bot, chat_id, "Siz kimsiniz…",
        #                            [("Elanın sahibi", "owner"),
        #                             ("Vasitəçi", "agent")])
        # is_owner = who == "owner"
        is_owner = False          # default: agent (Mən vasitəçiyəm)
        await flow.choose_owner(is_owner)

        # City — buttons from the known list; typed onto the page to select.
        city = await _pick_list(bot, chat_id, flow, "city_button", "Şəhər",
                                tag="city", known=CITIES)

        # Rayon (district): ONLY for Bakı, and it shows DISTRICT names, not cities.
        if (city or "").strip().lower() in ("bakı", "baki", "baku"):
            await _pick_list(bot, chat_id, flow, "district_button",
                             "Rayon", tag="district",
                             known=BAKU_DISTRICTS, optional=True)
            await _pick_list(bot, chat_id, flow, "village_button",
                             "Qəsəbə", tag="village", optional=True)

        address = await ask.ask_text(bot, chat_id,
                                     "Dəqiq ünvan (yerləşmə)?")

        # Map: optionally pin the location on the map and confirm the popup (#4).
        want_map = await ask.ask_choice(bot, chat_id,
                                        "Yeri xəritədə qeyd edək?",
                                        [("📍 Yes", "yes"), ("Skip", "no")])
        if want_map == "yes":
            ok = await flow.open_map_and_confirm()
            await bot.send_message(chat_id,
                "📍 Xəritə yeri təsdiqləndi." if ok else
                "⚠️ Couldn't auto-confirm the map — set it manually later if needed.")

        rooms = await ask.ask_text(bot, chat_id, "Otaq sayı?")
        area = await ask.ask_text(bot, chat_id, "Sahə (m²)?")
        floor = await ask.ask_text(bot, chat_id, "Mərtəbə?")
        total = await ask.ask_text(bot, chat_id, "Mərtəbələrin sayı?")

        repair = await ask.ask_choice(bot, chat_id, "Təmir?",
                                      [("Təmirli", "yes"), ("Təmirsiz", "no")])
        await flow.set_repair(repair == "yes")

        desc = await ask.ask_text(bot, chat_id,
                                  "Əlavə məlumat (təsvir). Telefon/e-mail yazmayın.")
        price = await ask.ask_text(bot, chat_id, "Qiymət (AZN)?")

        await flow.fill_details(address=address, rooms=rooms, area=area, floor=floor,
                                total_floors=total, description=desc, price=price)

        # optional checkboxes
        extras = await ask.ask_choice(bot, chat_id, "Bunlardan hər hansı biri varmı?",
                                      [("Çıxarış var", "bill"),
                                       ("İpoteka var", "mortgage"),
                                       ("Heç biri", "none")])
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
                await bot.send_message(chat_id, "İlk 30 şəkil istifadə olunur.")
            break
        await flow.add_photos(photos)

        # contact
        name = PUBLISHER_NAME or await ask.ask_text(bot, chat_id, "Adınız?")
        email = PUBLISHER_EMAIL or await ask.ask_text(bot, chat_id, "E-mail ünvanınız?")
        await flow.fill_contact(name=name, email=email, is_owner=is_owner)

        # review + submit
        summary = (f"<b>Yoxlama</b>\n"
                   f"• {cat} · Sell\n"
                   f"• {city} · {rooms} rooms · {area} m² · floor {floor}/{total}\n"
                   f"• Repair: {repair} · Price: {price} AZN\n"
                   f"• {len(photos)} photos\n\n"
                   f"Tap Continue to submit (bina.az may then show a package step).")
        go = await ask.ask_choice(bot, chat_id, summary,
                                  [("▶️ Davam etmək", "go")])
        if go != "go":
            raise Cancelled()

        final_url = await flow.submit()

    await bot.send_message(
        chat_id,
        "✅ <b>Elan göndərildi!</b>\n\n"
        "Your ad has been sent to bina.az for review. Once their moderators "
        "approve it, it goes live on the site.\n\n"
        "Statusu istənilən vaxt 📋 Elanlarım ilə yoxlayın.",
        reply_markup=main_menu())


# --------------------------------------------------------------------------
async def _otp_provider(bot: Bot, chat_id: int, state: FSMContext):
    async def provider(phone: str, attempt: int, error: str | None) -> str:
        lines = []
        if error:
            lines.append(f"⚠️ {error}")
        lines.append(f"📲 bina.az <b>{mask(phone)}</b> nömrəsinə SMS kod göndərdi.")
        lines.append("Kodu bura göndərin. Dayandırmaq üçün /cancel.")
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
            await bot.send_message(chat_id, "⏰ Kod vaxtında gəlmədi.", reply_markup=main_menu())
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
        await bot.send_message(chat_id, "✅ Artıq giriş edilib (yadda saxlanmış sessiya — SMS lazım deyil).")
    return True


async def run_login(bot: Bot, chat_id: int, user_id: int, phone: str, state: FSMContext):
    if lock_for(chat_id).locked():
        await bot.send_message(chat_id, "⏳ Məşğul — eyni anda bir əməliyyat.")
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
