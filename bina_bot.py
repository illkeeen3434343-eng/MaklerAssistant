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
# PUBLIC_MODE=true  -> anyone may /start; they register as 'pending' and an
#                      admin approves them (the approval system you already have).
# PUBLIC_MODE=false -> strict ALLOWED_USER_IDS whitelist (original behaviour).
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
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
    """Access gate.

    PUBLIC_MODE=false -> strict ALLOWED_USER_IDS whitelist (private bot).
    PUBLIC_MODE=true  -> anyone may /start. They are registered as 'pending'
                         and must be approved by an admin (Admin -> Set status)
                         before they can use any feature. 'blocked' users are
                         refused outright.
    """

    # Things a not-yet-approved user is still allowed to do.
    EXEMPT_TEXTS = {"/start", "/cancel", "/stop", "/help"}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        uid = user.id
        _current_uid.set(uid)
        _last_user_id[getattr(getattr(event, "chat", None), "id", uid) or uid] = uid

        # usage statistics (#2) — record button/command presses only
        try:
            t = (getattr(event, "text", "") or "").strip()
            if t and (t in MENU_TEXTS or t.startswith("/")):
                U.track(uid, t[:40])
        except Exception:
            pass

        # Admins always pass.
        if uid in ADMIN_IDS:
            return await handler(event, data)

        if not PUBLIC_MODE:
            if uid not in ALLOWED:
                await self._deny(event,
                    "⛔️ Bu bot şəxsidir. Giriş üçün admin ilə əlaqə saxlayın.")
                return None
            return await handler(event, data)

        # ---- public mode ----
        rec = U.ensure_user(uid, username=(user.username or ""),
                            name=(user.full_name or ""))
        status = rec.get("status", "pending")

        if status == "blocked":
            await self._deny(event, "⛔️ Hesabınız bloklanıb.")
            return None

        if status != "active":
            text = (getattr(event, "text", "") or "").strip()
            raw_state = data.get("raw_state")
            in_report = raw_state == Flow.report.state
            allowed_now = (
                text in self.EXEMPT_TEXTS
                or text == BTN_CONTACT
                or in_report
                or getattr(event, "photo", None) and in_report
            )
            if not allowed_now:
                await self._deny(event,
                    "⏳ Hesabınız təsdiq gözləyir.\n\n"
                    "Admin sizi aktivləşdirdikdən sonra botdan istifadə edə "
                    "biləcəksiniz. Müraciət üçün ✉️ Əlaqə düyməsindən istifadə edin.")
                return None

        return await handler(event, data)

    @staticmethod
    async def _deny(event, text: str):
        try:
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
        except Exception:
            pass


# --------------------------------------------------------------------------
BTN_LOGIN = "🔑 Giriş"
BTN_NEW = "➕ Yeni elan"
BTN_ADS = "📋 Elanlarım"
BTN_STATUS = "🩺 Status"
BTN_SESSIONS = "📱 Sessiyalar"
BTN_ADMIN = "🛠 Admin"

# sessions sub-menu
BTN_S_NEW = "➕ Yeni sessiya"
BTN_S_SWITCH = "🔀 Nömrələr arası keçid"
BTN_S_LIST = "📄 Nömrələrim"
BTN_S_FORGET = "🚪 Sessiyanı dayandır"
BTN_S_REMOVE = "🗑 Nömrəni sil"
BTN_CONTACT = "✉️ Əlaqə"
BTN_HELP = "❓ Kömək"

# admin sub-menu
BTN_A_PENDING = "⏳ Gözləyən istifadəçilər"
BTN_A_USERS = "👥 Bütün istifadəçilər"
BTN_A_SETSTATUS = "✅ Status təyin et"
BTN_A_SETTIER = "⭐ Tarif təyin et"
BTN_A_STATS = "📊 Statistika"
BTN_A_BACK = "⬅️ Geri"


_current_uid: contextvars.ContextVar = contextvars.ContextVar("uid", default=None)


def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    uid = user_id if user_id is not None else _current_uid.get()
    rows = [
        [KeyboardButton(text=BTN_NEW)],
        [KeyboardButton(text=BTN_LOGIN), KeyboardButton(text=BTN_SESSIONS)],
        [KeyboardButton(text=BTN_ADS), KeyboardButton(text=BTN_STATUS)],
        [KeyboardButton(text=BTN_CONTACT), KeyboardButton(text=BTN_HELP)],
    ]
    if uid is not None and uid in ADMIN_IDS:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               input_field_placeholder="Düymə seçin…")


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
            [KeyboardButton(text=BTN_A_STATS)],
            [KeyboardButton(text=BTN_A_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Admin…",
    )


# Tracks each user's currently-active bina.az number.
_active_number: dict[int, str] = {}
# which admin action was requested before tapping a /id  (#3)
_admin_intent: dict[int, str] = {}


async def _notify(bot: Bot, user_id: int, text: str) -> bool:
    """Send a notification to a user; True if delivered."""
    try:
        await bot.send_message(user_id, text)
        return True
    except Exception:
        return False


async def notify_admins(bot: Bot, text: str) -> int:
    sent = 0
    for admin_id in ADMIN_IDS:
        if await _notify(bot, admin_id, text):
            sent += 1
    return sent


async def report_error_to_admins(bot: Bot, user_id: int, where: str, err) -> None:
    """#1 — summarise a user-facing failure for the admins."""
    try:
        await notify_admins(
            bot,
            "⚠️ <b>İstifadəçidə xəta</b>\n"
            f"Kim: {U.label_for(user_id)}\n"
            f"Harada: <b>{where}</b>\n"
            f"Xəta: <code>{str(err)[:300]}</code>")
    except Exception:
        pass


def active_phone(user_id: int) -> str | None:
    """This user's active bina.az number — or None.

    SECURITY: never fall back to the global BINA_PHONE (.env) for someone who
    has not connected a number themselves. That value is the operator's own
    number; leaking it made every new user see the admin's number as "active"
    and could have let them act on the admin's session.
    """
    if user_id in _active_number:
        return _active_number[user_id]
    nums = U.numbers(user_id)
    if nums:
        return nums[0]
    # Only the operator/admins may use the .env convenience number.
    if user_id in ADMIN_IDS and BINA_PHONE:
        return BINA_PHONE
    return None


def phone_for(state_phone: str | None) -> str | None:
    if state_phone:
        return state_phone
    uid = _current_uid.get()
    if uid is None:
        return None
    return active_phone(uid)


# --------------------------------------------------------------------------
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid = msg.from_user.id
    uname = msg.from_user.username or ""
    fname = msg.from_user.full_name or ""
    is_new = U.get_user(uid) is None
    U.ensure_user(uid, default_status="active" if uid in ADMIN_IDS else "pending",
                  username=uname, name=fname)
    if uid in ADMIN_IDS and not U.is_active(uid):
        U.set_status(uid, "active")
    # Tell the admins a new person is waiting for approval.
    if is_new and uid not in ADMIN_IDS:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "🆕 <b>Yeni istifadəçi qeydiyyatdan keçdi</b>\n"
                    f"{U.label_for(uid)}\n\n"
                    "Təsdiqləmək üçün ID-yə toxunun.")
            except Exception:
                pass
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
        await msg.answer(f"debug/ qovluğu yoxdur: <code>{debug_dir}</code> — "
                         "heç bir xəta olmayıb.")
        return
    files = sorted(debug_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        await msg.answer(f"<code>{debug_dir}</code> boşdur.")
        return
    await msg.answer(f"debug/ qovluğunda {len(files)} fayl var. Ən yenilər göndərilir…")
    sent = 0
    for f in files[:6]:
        try:
            await msg.answer_document(FSInputFile(str(f)), caption=f.name)
            sent += 1
        except Exception as exc:
            await msg.answer(f"{f.name} göndərilə bilmədi: {exc}")
    await msg.answer(f"{sent} fayl göndərildi. <code>*-open.html</code> faylları "
                     "açılmış siyahıları göstərir.")


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
async def wizard_photo(msg: Message, bot: Bot, state: FSMContext):
    # Report photos belong to the Contact flow — let that handler have them.
    if await state.get_state() == Flow.report.state:
        await _handle_report_photo(msg, state, bot)
        return
    # admin composing a message to a user (text OR photo)
    if ask.waiting_kind(msg.chat.id) == "message":
        ask.feed_message_photo(msg.chat.id, msg.photo[-1].file_id,
                               msg.caption or "")
        return
    if ask.waiting_kind(msg.chat.id) != "photos":
        return
    # download the largest size to a temp file, hand the path to the broker
    import tempfile
    photo = msg.photo[-1]
    path = str(Path(tempfile.gettempdir()) / f"binaphoto_{photo.file_unique_id}.jpg")
    try:
        await bot.download(photo, destination=path)
        ask.feed_photo(msg.chat.id, path)
        await msg.answer("📷 Minimum 4, maksimum 30 ədəd şəkil yükləyin.\n")
    except Exception as exc:
        await msg.answer(f"şəkli saxlaya bilmədim: {exc}")


MENU_TEXTS = {BTN_LOGIN, BTN_NEW, BTN_ADS, BTN_STATUS, BTN_SESSIONS, BTN_ADMIN,
              BTN_HELP, BTN_A_STATS,
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
        await msg.answer("📱 Nömrənizi daxil edin (məs. <code>557778899</code>):")
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


async def _handle_report_photo(msg: Message, state: FSMContext, bot: Bot):
    """Forward a photo report to the admins (shared by both photo handlers)."""
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


@dp.message(Flow.report, F.photo)
async def got_report_photo(msg: Message, state: FSMContext, bot: Bot):
    await _handle_report_photo(msg, state, bot)


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
    await msg.answer("📱 <b>Sessiyalar</b> — aktiv nömrələrinizi idarə edin.",
                     reply_markup=sessions_menu())


@dp.message(F.text == BTN_S_LIST)
async def kb_s_list(msg: Message):
    uid = msg.from_user.id
    nums = U.numbers(uid)
    act = active_phone(uid)
    lines = [f"<b>Your numbers</b> (tier {U.tier_of(uid)}, "
             f"max {U.max_numbers(uid)})", ""]
    if not nums and not act:
        lines.append("None yet — use Yeni sessiya.")
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
        await msg.answer("Sizin hesabınız hələ ki aktivləşdirilmiyib.", reply_markup=sessions_menu())
        return
    cap = U.max_numbers(uid); have = len(U.numbers(uid))
    if have >= cap:
        await msg.answer(
            f"Sizin ({U.tier_of(uid)}) abunəliyiniz {cap} ədəd nömrənin "
            f"istifadə edilməsinə imkan verir. Sizin aktiv {have} nömrəniz "
            f"mövcuddur. Abunəliyinizi dəyişmək üçün adminlə əlaqə saxlayın.",
            reply_markup=sessions_menu())
        return
    await state.set_state(Flow.ask_phone)
    await msg.answer("➕ Nömrənizi daxil edin (Nümunə: <code>0701112233</code>). "
                     "Giriş üçün təsdiq kodu göndəriləcək.")


@dp.message(F.text == BTN_S_SWITCH)
async def kb_s_switch(msg: Message, bot: Bot, state: FSMContext):
    uid = msg.from_user.id
    nums = U.numbers(uid)
    if len(nums) < 2:
        await msg.answer("Cəmi bir nömrəniz var. ➕ Yeni sessiya ilə başqa nömrə əlavə edin.",
                         reply_markup=sessions_menu())
        return
    opts = [(mask(n), n) for n in nums]
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Aktiv proses mövcuddur. Yenisi üçün əvvəlki əməliyyatı bitirin.")
        return
    async with lock_for(chat_id):
        try:
            chosen = await ask.ask_choice(bot, chat_id, "Aktiv nömrəni dəyişin:", opts)
            _active_number[uid] = chosen
            await bot.send_message(chat_id, f"Hazırda aktiv olan nömrəniz: {mask(chosen)}",
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
        "use Nömrəni sil.</i>",
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
        await msg.answer("⏳ Aktiv proses mövcuddur. Yenisi üçün əvvəlki əməliyyatı bitirin.")
        return
    async with lock_for(chat_id):
        try:
            opts = [(mask(n), n) for n in nums]
            chosen = await ask.ask_choice(bot, chat_id,
                "Silmək istədiyniz nömrəni seçin.", opts)
            # clear its saved session, drop it from the user's list
            sess = get_session(uid, chosen)
            sess.forget()
            await sess.close()
            U.remove_number(uid, chosen)
            _active_number.pop(uid, None)
            await bot.send_message(chat_id,
                f"🗑 {mask(chosen)} nömrəniz hesabdan silindi.\n\n"
                "<i>Silinən nömrə sizə məxsus olduğu üçün başqa istifadəçi "
                "tərəfindən istifadə edilə bilməz. Əgər nömrə artıq sizə aid "
                "deyilsə, nömrənin adınızdan silinməsi üçün admin ilə əlaqə "
                "saxlayın.</i>",
                reply_markup=sessions_menu())
        except Cancelled:
            await bot.send_message(chat_id, "Ləğv edildi.", reply_markup=sessions_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Vaxt bitdi.", reply_markup=sessions_menu())
# ==================== END SESSIONS SUBMENU ====================


# ==================== KÖMƏK (#9) ====================
HELP_TEXT = """❓ <b>MaklerAssistant — kömək</b>

Bu bot bina.az hesabınızı Telegram üzərindən idarə etməyə imkan verir: elan
yerləşdirmək və elanlarınıza baxmaq.

<b>1. Necə işləyir?</b>
Bot sizin adınızdan bina.az saytına daxil olur. Giriş üçün bina.az sizin
telefonunuza SMS kod göndərir, siz kodu bota yazırsınız, bot girişi tamamlayır.
Sessiya yadda saxlanılır — hər dəfə SMS lazım olmur (adətən bir neçə gün).

<b>2. Başlamaq</b>
• Qeydiyyat: /start yazın. Hesabınız <b>təsdiq gözləyir</b> statusunda olur.
• Admin sizi aktivləşdirdikdən sonra bütün funksiyalar açılır.
• Sonra 📱 Sessiyalar → ➕ Yeni sessiya ilə bina.az nömrənizi qoşun.

<b>3. Düymələr</b>
• ➕ <b>Yeni elan</b> — addım-addım yeni elan yerləşdirir (növ, şəhər, rayon,
  ünvan, otaq, sahə, mərtəbə, təmir, şəkil, qiymət, əlaqə).
• 📋 <b>Elanlarım</b> — bina.az-dakı elanlarınızı göstərir (qəbul olunmayanlar
  daxil olmaqla).
• 🔑 <b>Giriş</b> — hesaba daxil olur / sessiyanı yoxlayır.
• 📱 <b>Sessiyalar</b> — nömrələrinizi idarə edin:
   ➕ Yeni sessiya · 🔀 Nömrəni dəyiş · 📄 Nömrələrim
   🚪 Sessiyanı unut (yalnız girişi silir, nömrə qalır)
   🗑 Nömrəni sil (nömrəni hesabınızdan çıxarır)
• 🩺 <b>Status</b> — aktiv nömrə, sessiya və tarif məlumatı.
• ✉️ <b>Əlaqə</b> — admin ilə əlaqə (mətn və ya şəkil göndərə bilərsiniz).

<b>4. Şəkillər</b>
bina.az minimum <b>4</b>, maksimum <b>30</b> şəkil tələb edir. Skrinşot, loqolu,
çərçivəli və ya bulanıq şəkillər qəbul edilmir.

<b>5. Tariflər</b>
Pulsuz — 1 nömrə · Pro — 2 nömrə · Diamond — 5 nömrə.
Tarifi dəyişmək üçün admin ilə əlaqə saxlayın.

<b>6. Elan statusu</b>
Elan göndərildikdən sonra bina.az moderatorları yoxlayır. Nəticəni
📋 Elanlarım bölməsində görə bilərsiniz.

<b>7. Problem olarsa</b>
✉️ Əlaqə düyməsi ilə yazın — mesajınız (və şəkil) birbaşa adminə gedir.
İstənilən addımı dayandırmaq üçün /cancel yazın."""


@dp.message(F.text == BTN_HELP)
@dp.message(Command("help"))
async def kb_help(msg: Message):
    await msg.answer(HELP_TEXT, reply_markup=main_menu(msg.from_user.id))


# ==================== STATİSTİKA (#2) ====================
@dp.message(F.text == BTN_A_STATS)
async def kb_admin_stats(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    s = U.stats_summary()
    users = U.all_users()
    by_status: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    numbers = 0
    for r in users.values():
        by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1
        by_tier[r.get("tier", "free")] = by_tier.get(r.get("tier", "free"), 0) + 1
        numbers += len(r.get("numbers", []))

    lines = ["📊 <b>Statistika</b>", ""]
    lines.append(f"👥 İstifadəçi: <b>{len(users)}</b> · bu gün aktiv: "
                 f"<b>{s['active_today']}</b>")
    lines.append("   " + " · ".join(f"{k}: {v}" for k, v in by_status.items()))
    lines.append("   Tarif — " + " · ".join(f"{k}: {v}" for k, v in by_tier.items()))
    lines.append(f"📱 Qoşulmuş nömrə: <b>{numbers}</b>")
    lines.append(f"🖱 Ümumi əməliyyat: <b>{s['total_actions']}</b>")

    if s["top_actions"]:
        lines += ["", "<b>Ən çox istifadə olunan düymələr</b>"]
        for name, cnt in s["top_actions"]:
            lines.append(f"   {cnt:>4} × {name}")

    if s["top_users"]:
        lines += ["", "<b>Ən aktiv istifadəçilər</b>"]
        for uid, total, last in s["top_users"]:
            if not total:
                continue
            lines.append(f"   {U.label_for(int(uid))} — {total} əməliyyat "
                         f"(son: {last.replace('T', ' ')[:16]})")

    await msg.answer("\n".join(lines)[:3800], reply_markup=admin_menu())


# ==================== ADMIN PANEL (#5, #6, #7) ====================
def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


@dp.message(F.text == BTN_ADMIN)
async def kb_admin(msg: Message):
    if not _is_admin(msg.from_user.id):
        await msg.answer("⛔️ Yalnız adminlər üçün.")
        return
    # #7: main buttons -> tap Admin -> admin buttons + Back appear
    await msg.answer("🛠 <b>Admin paneli</b>\nİstifadəçiləri, statusları və tarifləri idarə edin.",
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
async def kb_admin_users(msg: Message, header: str | None = None):
    if not _is_admin(msg.from_user.id):
        return
    data = U.all_users()
    if not data:
        await msg.answer("Hələ istifadəçi yoxdur.", reply_markup=admin_menu())
        return
    lines = [header or "👥 <b>Bütün istifadəçilər</b>",
             "İdarə etmək üçün ID-yə toxunun:", ""]
    for u, r in data.items():
        lines.append(_user_line(u, r))
    await msg.answer("\n".join(lines)[:3800], reply_markup=admin_menu())


@dp.message(F.text == BTN_A_SETSTATUS)
async def kb_admin_setstatus(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    _admin_intent[msg.from_user.id] = "status"
    await kb_admin_users(msg, header="✅ <b>Status təyin et</b> — istifadəçi ID-sinə toxunun:")


@dp.message(F.text == BTN_A_SETTIER)
async def kb_admin_settier(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    _admin_intent[msg.from_user.id] = "tier"
    await kb_admin_users(msg, header="⭐ <b>Tarif təyin et</b> — istifadəçi ID-sinə toxunun:")


# Tapping a "/123456" line opens an action menu for that user (admin only).
@dp.message(F.text.regexp(r"^/\d{4,}$"))
async def admin_pick_user(msg: Message, bot: Bot):
    if not _is_admin(msg.from_user.id):
        return
    admin_id = msg.from_user.id
    target = int(msg.text.strip().lstrip("/"))
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Aktiv proses mövcuddur. Yenisi üçün əvvəlki əməliyyatı bitirin.")
        return
    intent = _admin_intent.pop(admin_id, None)
    async with lock_for(chat_id):
        try:
            U.ensure_user(target)
            if intent == "status":
                opts = [("✅ Aktiv et", "st:active"), ("⏳ Gözləmə", "st:pending"),
                        ("🚫 Blokla", "st:blocked")]
            elif intent == "tier":
                opts = [("⭐ Pulsuz (1 nömrə)", "ti:free"),
                        ("⭐ Pro (2 nömrə)", "ti:pro"),
                        ("⭐ Diamond (5 nömrə)", "ti:diamond")]
            else:
                opts = [("✅ Status dəyiş", "go:status"),
                        ("⭐ Tarif dəyiş", "go:tier"),
                        ("📱 Nömrələri idarə et", "go:numbers"),
                        ("✉️ Mesaj göndər", "go:msg")]
            action = await ask.ask_choice(
                bot, chat_id, f"{U.label_for(target)} — nə edək?", opts)
            kind, val = action.split(":", 1)

            if kind == "go":
                if val == "status":
                    _admin_intent[admin_id] = "status"
                elif val == "tier":
                    _admin_intent[admin_id] = "tier"
                if val in ("status", "tier"):
                    await bot.send_message(
                        chat_id, f"Yenidən <code>/{target}</code> yazın "
                                 f"və ya ID-yə toxunun.", reply_markup=admin_menu())
                    return
                if val == "numbers":
                    await _admin_manage_numbers(bot, chat_id, target)
                    return
                if val == "msg":
                    await _admin_message_user(bot, chat_id, target)
                    return

            if kind == "st":
                U.set_status(target, val)
                az = {"active": "Aktiv", "pending": "Gözləmədə",
                      "blocked": "Bloklandı"}[val]
                await bot.send_message(chat_id, f"✅ {U.label_for(target)} → {az}",
                                       reply_markup=admin_menu())
                await _notify(bot, target,
                              f"ℹ️ Admin statusunuzu dəyişdi: <b>{az}</b>."
                              + ("\n\nArtıq botdan istifadə edə bilərsiniz. /start"
                                 if val == "active" else ""))
                if val == "active":
                    await _ask_expiry(bot, chat_id, target)
                else:
                    U.set_expiry(target, None)
            elif kind == "ti":
                U.set_tier(target, val)
                cap = U.TIERS[val]["max_numbers"]
                await bot.send_message(chat_id, f"⭐ {U.label_for(target)} → {val}",
                                       reply_markup=admin_menu())
                await _notify(bot, target,
                              f"⭐ Tarifiniz dəyişdirildi: <b>{val}</b> "
                              f"({cap} nömrə).")
                await _ask_expiry(bot, chat_id, target)
        except Cancelled:
            await bot.send_message(chat_id, "Ləğv edildi.", reply_markup=admin_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Vaxt bitdi.", reply_markup=admin_menu())


async def _admin_manage_numbers(bot: Bot, chat_id: int, target: int):
    """#8 — assign a number to a user, or unassign one from them."""
    nums = U.numbers(target)
    listing = "\n".join(f"• {mask(n)}" for n in nums) or "<i>nömrə yoxdur</i>"
    act = await ask.ask_choice(
        bot, chat_id,
        f"{U.label_for(target)}\nNömrələr:\n{listing}\n\nNə edək?",
        [("➕ Nömrə təyin et", "add"), ("➖ Nömrəni ayır", "del")])
    if act == "add":
        phone = (await ask.ask_text(
            bot, chat_id, "Təyin ediləcək nömrəni yazın (məs. 0557778899):")).strip()
        owner = security.owner_of(phone)
        if owner is not None and str(owner) != str(target):
            await bot.send_message(
                chat_id,
                f"⚠️ Bu nömrə artıq <code>{owner}</code> istifadəçisinə aiddir. "
                f"Əvvəlcə ondan ayırın.", reply_markup=admin_menu())
            return
        security.claim(phone, target)
        ok, note = U.add_number(target, phone)
        await bot.send_message(
            chat_id,
            f"✅ {mask(phone)} → {U.label_for(target)}" if ok else f"⚠️ {note}",
            reply_markup=admin_menu())
        if ok:
            await _notify(bot, target,
                          f"📱 Admin hesabınıza yeni nömrə əlavə etdi: {mask(phone)}")
    else:
        if not nums:
            await bot.send_message(chat_id, "Bu istifadəçinin nömrəsi yoxdur.",
                                   reply_markup=admin_menu())
            return
        chosen = await ask.ask_choice(bot, chat_id, "Hansı nömrəni ayıraq?",
                                      [(mask(n), n) for n in nums])
        security.release(chosen, target)
        U.remove_number(target, chosen)
        try:
            sess = get_session(target, chosen)
            sess.forget()
            await sess.close()
        except Exception:
            pass
        _active_number.pop(target, None)
        await bot.send_message(chat_id,
                               f"➖ {mask(chosen)} ayrıldı ({U.label_for(target)}).",
                               reply_markup=admin_menu())
        await _notify(bot, target,
                      f"📱 Admin {mask(chosen)} nömrəsini hesabınızdan ayırdı.")


async def _ask_expiry(bot: Bot, chat_id: int, target: int):
    """#1 — after approving/upgrading, record when the subscription ends."""
    from datetime import date, timedelta
    today = date.today()
    opts = [
        ("1 ay", (today + timedelta(days=30)).isoformat()),
        ("3 ay", (today + timedelta(days=90)).isoformat()),
        ("6 ay", (today + timedelta(days=180)).isoformat()),
        ("1 il", (today + timedelta(days=365)).isoformat()),
        ("📅 Tarix yazım", "manual"),
        ("Müddətsiz", "none"),
    ]
    try:
        choice = await ask.ask_choice(
            bot, chat_id,
            f"{U.label_for(target)} — abunə nə vaxt bitir?", opts)
    except Exception:
        return
    if choice == "none":
        U.set_expiry(target, None)
        await bot.send_message(chat_id, "♾ Müddətsiz olaraq qeyd edildi.",
                               reply_markup=admin_menu())
        return
    if choice == "manual":
        raw = (await ask.ask_text(
            bot, chat_id, "Bitmə tarixini yazın (<b>YYYY-MM-DD</b>):")).strip()
        try:
            date.fromisoformat(raw)
        except Exception:
            await bot.send_message(chat_id, "⚠️ Tarix formatı yanlışdır.",
                                   reply_markup=admin_menu())
            return
        choice = raw
    U.set_expiry(target, choice)
    await bot.send_message(chat_id,
                           f"📅 Abunə bitmə tarixi: <b>{choice}</b>",
                           reply_markup=admin_menu())
    await _notify(bot, target, f"📅 Abunəniz <b>{choice}</b> tarixinədək aktivdir.")


async def subscription_watcher(bot: Bot):
    """Daily: warn 3 days before expiry, and downgrade on expiry (#1)."""
    while True:
        try:
            soon, gone = U.subscriptions_due(warn_days=3)
            for uid, exp, left in soon:
                gun = "bu gün" if left == 0 else f"{left} gün sonra"
                await _notify(bot, uid,
                    f"⏳ <b>Abunə bitmək üzrədir</b>\n"
                    f"Abunəniz {gun} ({exp}) başa çatır.\n"
                    f"Davam etmək üçün admin ilə əlaqə saxlayın (✉️ Əlaqə).")
                await notify_admins(bot,
                    f"⏳ <b>Abunə bitir</b>\n{U.label_for(uid)}\n"
                    f"Tarix: {exp} ({gun}). Ödəniş alındıqda tarixi yeniləyin.")
                U.mark_expiry_warned(uid)
            for uid, exp in gone:
                U.set_status(uid, "pending")
                U.set_expiry(uid, None)
                await _notify(bot, uid,
                    f"🔒 <b>Abunəniz bitdi</b> ({exp}).\n"
                    f"Hesabınız təsdiq gözləmə rejiminə keçirildi. "
                    f"Yeniləmək üçün ✉️ Əlaqə ilə admin ilə danışın.")
                await notify_admins(bot,
                    f"🔒 <b>Abunə bitdi</b>\n{U.label_for(uid)} ({exp})\n"
                    f"Status avtomatik <b>gözləmədə</b> edildi.")
        except Exception as exc:
            _log(f"subscription watcher error: {exc}")
        await asyncio.sleep(6 * 3600)     # check 4x/day


async def _admin_message_user(bot: Bot, chat_id: int, target: int):
    """#4 — admin sends a direct message (text OR photo) to a user."""
    await bot.send_message(
        chat_id,
        f"{U.label_for(target)} istifadəçisinə göndəriləcək mesajı yazın "
        f"— <b>şəkil də göndərə bilərsiniz</b>.")
    got = await ask.ask_message(bot, chat_id)      # text or photo
    if got is None:
        await bot.send_message(chat_id, "Boş mesaj göndərilmədi.",
                               reply_markup=admin_menu())
        return
    kind, payload, caption = got
    ok = False
    try:
        if kind == "photo":
            await bot.send_photo(
                target, payload,
                caption=f"✉️ <b>Admindən mesaj</b>\n\n{caption}"[:1024])
        else:
            await bot.send_message(target, f"✉️ <b>Admindən mesaj</b>\n\n{payload}")
        ok = True
    except Exception:
        ok = False
    await bot.send_message(
        chat_id,
        "✅ Göndərildi." if ok else "⚠️ İstifadəçiyə çatdırıla bilmədi "
                                    "(botu bloklamış ola bilər).",
        reply_markup=admin_menu())
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
        f"Yadda saxlanan sessiya: {'🟢 var' if saved else '⚪️ yoxdur (SMS lazımdır)'}\n"
        f"Şifrələmə: {enc} · sizə aiddir: {'bəli' if owner else 'təyin olunmayıb'}",
        reply_markup=main_menu())


@dp.message(F.text == BTN_ADS)
async def kb_ads(msg: Message, bot: Bot, state: FSMContext):
    phone = phone_for(None)
    if not phone:
        await msg.answer("Əvvəlcə nömrə daxil edin (🔑 Giriş).")
        return
    chat_id = msg.chat.id
    if lock_for(chat_id).locked():
        await msg.answer("⏳ Məşğul — eyni anda bir əməliyyat icra oluna bilər.")
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
            await bot.send_message(chat_id, f"Elanlarınız barədə məlumat əldə edilə bilmədi: {exc}",
                                   reply_markup=main_menu())
            return
    if not ads:
        await bot.send_message(chat_id, "Bu hesabda heç bir elan yoxdur.",
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
        await msg.answer("Əvvəlcə nömrə daxil edin (🔑 Giriş).")
        return
    if lock_for(msg.chat.id).locked():
        await msg.answer("⏳ Məşğul — eyni anda bir əməliyyat icra oluna bilər.")
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
            await bot.send_message(msg.chat.id, "⏰ Cavab gözləmə müddəti bitdi.", reply_markup=main_menu())
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
        await call.message.answer(
            "📱 Nömrənizi daxil edin (Nümunə: <code>0701112233</code>).")
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
        f"Yadda saxlanan sessiya: {'🟢 var' if saved else '⚪️ yoxdur (SMS lazımdır)'}\n"
        f"Şifrələmə: {enc} · sizə aiddir: {'bəli' if owner else 'təyin olunmayıb'}",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "logout")
async def cb_logout(call: CallbackQuery):
    await call.answer("Dayandırılmış sessiyalar")
    phone = phone_for(None)
    user_id = call.from_user.id
    if phone and (security.owner_of(phone) in (None, user_id) or
                  str(security.owner_of(phone)) == str(user_id)):
        sess = get_session(user_id, phone)
        sess.forget()
        await sess.close()
    await call.message.answer(
        "🚪 Yadda saxlanan sessiya silindi. Növbəti girişdə təsdiq kodu lazım olacaq.\n"
        "<i>(Sessiya dayandırılsa da nömrə sizə məxsusdur.)</i>",
        reply_markup=main_menu())


@dp.callback_query(F.data == "myads")
async def cb_myads(call: CallbackQuery, bot: Bot, state: FSMContext):
    await call.answer()
    phone = phone_for(None)
    if not phone:
        await call.message.answer("Əvvəlcə nömrə daxil edin (🔑 Giriş).")
        return
    chat_id = call.message.chat.id
    if lock_for(chat_id).locked():
        await call.message.answer("⏳ Məşğul — eyni anda bir əməliyyat icra oluna bilər.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, call.from_user.id, phone, state)
        if not ok:
            return
        await call.message.answer("📋 Giriş uğurludur.",
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
        await call.message.answer("Əvvəlcə nömrə daxil edin (🔑 Giriş).")
        return
    chat_id = call.message.chat.id
    if lock_for(chat_id).locked():
        await call.message.answer("⏳ Məşğul — eyni anda bir əməliyyat icra oluna bilər.")
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
            await bot.send_message(chat_id, f"❌ {exc}\nDebug şəkli saxlanıldı.",
                                   reply_markup=main_menu())
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Cavab gözləmə müddəti bitdi.", reply_markup=main_menu())
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
        await bot.send_message(chat_id, f"ℹ️ {tag} üçün hazır siyahı istifadə olunur.")
        options = list(known)
    if not options:
        if optional:
            await bot.send_message(
                chat_id, f"ℹ️ {tag} variantları oxuna bilmədi — mövcud dəyər saxlanılır.")
            return None
        raise PublishError(
            f"{tag} siyahısı açıldı, lakin variant tapılmadı.")
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
        await bot.send_message(chat_id, f"⚠️ '{chosen_text}' seçilə bilmədi — mövcud {tag} dəyəri saxlanılır.")
    return chosen_text


async def _ask_from_list(bot, chat_id, label, options, page_size=5):
    """Ask the user to pick from a known list of names (buttons only)."""
    page = 0
    while True:
        chunk = options[page * page_size:(page + 1) * page_size]
        opts = [(r[:40], f"r{page*page_size+i}") for i, r in enumerate(chunk)]
        if (page + 1) * page_size < len(options):
            opts.append(("➡️ Digər", "more"))
        chosen = await ask.ask_choice(bot, chat_id, f"{label}:", opts)
        if chosen == "more":
            page += 1
            continue
        return options[int(chosen[1:])]


async def _select_on_page(bot, chat_id, flow, opener_key, label, pick, tag,
                          already_open=False, optional=False):
    """Type `pick` into that field's own dropdown and click the matching row."""
    try:
        if already_open:
            results = await flow.type_in_open(tag, pick)
        else:
            results = await flow.search_and_pick(opener_key, pick, tag=tag)
        target = pick if pick in results else (results[0] if results else None)
        if target:
            await flow.pick_result(target, tag=tag)
            return pick
        if not optional:
            await bot.send_message(
                chat_id, f"⚠️ {label}: '{pick}' səhifədə tapılmadı.")
    except PublishError:
        if not optional:
            await bot.send_message(chat_id, f"⚠️ {label}: '{pick}' seçilə bilmədi.")
    return pick


async def _pick_list(bot, chat_id, flow, opener_key, label, tag,
                     known=None, optional=False, page_size=5):
    """Show the option buttons (from a known list), then select on the page.

    The choice buttons come from `known` (guaranteed-correct names). Once the
    user picks, we open the dropdown, TYPE that name into its search box, and
    click the matching radio row — which is robust and avoids relying on
    scraping the full list. Falls back to live scraping if no known list.
    """
    options = list(known) if known else await flow.search_and_pick(opener_key, "", tag=tag)
    if not options:
        if optional:
            await bot.send_message(chat_id, f"{label}: variant tapılmadı — keçilir.")
            return None
        raise PublishError(f"{label}: variant tapılmadı.")

    page = 0
    while True:
        chunk = options[page * page_size:(page + 1) * page_size]
        opts = [(r[:40], f"r{page*page_size+i}") for i, r in enumerate(chunk)]
        if (page + 1) * page_size < len(options):
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
                f"⚠️ '{pick}' səhifədə tapılmadı — davam edirəm.")
    except PublishError:
        if not optional:
            await bot.send_message(chat_id, f"⚠️ '{pick}' seçilə bilmədi.")
    return pick


async def publish_wizard(bot: Bot, chat_id: int, sess: BinaSession):
    flow = PublishFlow(sess)
    await bot.send_message(chat_id, "🏗 <b>Yeni elan</b>. Hər sual üçün yalnız 1 seçim edin.")

    async with sess.lock:
        await flow.open_new_ad()

        # Deal type is always SELL (this bot is for selling) — set silently.
        await flow.choose_deal(sell=True)

        # Category = property type. Asked ONCE here; sets the type dropdown.
        cat = await ask.ask_choice(bot, chat_id, "Əmlakın növünü seçin:",
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

        # City — ask which city, but SKIP touching the page when bina.az has
        # already selected it (the form defaults to Bakı, readonly). Re-typing
        # it was what left the field in a broken state.
        city = await _ask_from_list(bot, chat_id, "Şəhər", CITIES)
        already = (await flow.current_city()).strip()
        if already and already.casefold() == (city or "").casefold():
            await bot.send_message(chat_id, f"🏙 Şəhər artıq seçilib: <b>{already}</b>")
        else:
            await _select_on_page(bot, chat_id, flow, "city_button", "Şəhər",
                                  city, tag="city")

        # Rayon (district): ONLY for Bakı, and it shows DISTRICT names, not cities.
        if (city or "").strip().lower() in ("bakı", "baki", "baku"):
            district = await _ask_from_list(bot, chat_id, "Rayon",
                                            BAKU_DISTRICTS,
                                            page_size=len(BAKU_DISTRICTS))
            if not await flow.open_district():
                await bot.send_message(
                    chat_id, "⚠️ Rayon siyahısı açılmadı. /debug yazıb "
                             "publish-district-noopen.html göndərin.")
            else:
                await _select_on_page(bot, chat_id, flow, "district_button",
                                      "Rayon", district, tag="district",
                                      already_open=True)
            await _pick_list(bot, chat_id, flow, "village_button",
                             "Qəsəbə", tag="village", optional=True)

        address = await ask.ask_text(bot, chat_id,
                                     "Ünvanı daxil edin:")

        # Xəritədə yer qeydiyyatı MƏCBURİDİR — soruşmadan avtomatik edilir (#6).
        try:
            if not await flow.open_map_and_confirm():
                await bot.send_message(
                    chat_id, "⚠️ Xəritədə yeri təsdiqləyə bilmədim — davam edirəm.")
        except Exception:
            pass

        rooms = await ask.ask_text(bot, chat_id, "Otaq sayını daxil edin:")
        area = await ask.ask_text(bot, chat_id, "Sahəni daxil edin (m²):")
        floor = await ask.ask_text(bot, chat_id, "Mənzilin yerləşdiyi mərtəbəni daxil edin:")
        total = await ask.ask_text(bot, chat_id, "Binanın neçə mərtəbə olduğunu daxil edin:")

        repair = await ask.ask_choice(bot, chat_id, "Təmirin növünü seçin:",
                                      [("Təmirli", "yes"), ("Təmirsiz", "no")])
        await flow.set_repair(repair == "yes")

        desc = await ask.ask_text(bot, chat_id,
                                  "Elan barədə açıqlamanı qeyd edin:")
        price = await ask.ask_text(bot, chat_id, "Mənzilin satış qiymətini daxil edin:")

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
                "📷 Minimum 4, maksimum 30 ədəd şəkil yükləyin. "
                "Bitirdikdə ✅ düyməsinə basın.\n"
                "<i>Skrinşot, loqolu, çərçivəli və ya bulanıq şəkillər qəbul edilmir.</i>")
            if len(photos) < 4:
                await bot.send_message(chat_id,
                    f"Cəmi {len(photos)} ədəd şəkil yüklənib. "
                    "Minimum 4 ədəd şəkil olmalıdır.")
                continue
            if len(photos) > 30:
                photos = photos[:30]
                await bot.send_message(chat_id, "Minimum 4, maksimum 30 ədəd şəkil yükləyin.")
            break
        await flow.add_photos(photos)

        # contact
        name = PUBLISHER_NAME or await ask.ask_text(bot, chat_id, "Adınızı daxil edin:")
        email = PUBLISHER_EMAIL or await ask.ask_text(bot, chat_id, "E-mail ünvanınızı daxil edin:")
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
            "🔒 Bu nömrə artıq başqa istifadəçi tərəfindən daxil edilib. "
            "Təhlükəsizlik səbəbi ilə bir nömrə yalnız bir istifadəçi "
            "tərəfindən istifadə edilə bilər.",
            reply_markup=main_menu(),
        )
        return False

    sess = get_session(user_id, phone)
    async with sess.lock:
        try:
            fresh = await sess.ensure_logged_in(await _otp_provider(bot, chat_id, state))
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "⏰ Kodu vaxtında daxil etmədiniz.", reply_markup=main_menu())
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
        await bot.send_message(chat_id, "✅ Artıq giriş edilib (aktiv sessiyanız olduğu üçün birdəfəlik kod lazım deyil).")
    return True


async def run_login(bot: Bot, chat_id: int, user_id: int, phone: str, state: FSMContext):
    if lock_for(chat_id).locked():
        await bot.send_message(chat_id, "⏳ Məşğul — eyni anda bir əməliyyat icra oluna bilər.")
        return
    async with lock_for(chat_id):
        ok = await ensure_login(bot, chat_id, user_id, phone, state, announce_reused=True)
        if ok:
            await bot.send_message(
                chat_id,
                f"✅ {mask(phone)} nömrə ilə giriş edildi.\n"
                "Artıq aktiv sessiya yaradıldığı üçün növbəti giriş zamanı "
                "təsdiq kodu istənilməyəcək.",
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
        asyncio.create_task(subscription_watcher(bot))
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
