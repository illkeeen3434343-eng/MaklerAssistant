"""
Ask broker — lets a single linear coroutine drive a multi-step Telegram
conversation, awaiting the user's next answer at each step.

Same idea as the OTP relay, generalised to three kinds of prompt:
  • ask_text   → wait for a typed message
  • ask_choice → send inline buttons, wait for a tap
  • ask_photos → collect photos until the user taps "Done"

The bot's message/callback handlers route incoming updates into whichever
future the wizard is currently awaiting. This keeps the wizard readable as a
top-to-bottom script instead of a sprawl of FSM states.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


class Cancelled(Exception):
    pass


class _Pending:
    __slots__ = ("kind", "future", "photos")

    def __init__(self, kind: str):
        self.kind = kind                       # 'text' | 'choice' | 'photos'
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.photos: list[str] = []


class AskBroker:
    def __init__(self):
        self._pending: dict[int, _Pending] = {}

    def waiting_kind(self, chat_id: int) -> str | None:
        p = self._pending.get(chat_id)
        return p.kind if p and not p.future.done() else None

    # ---- called by the bot's handlers -----------------------------------
    def feed_text(self, chat_id: int, text: str) -> bool:
        p = self._pending.get(chat_id)
        if not p or p.future.done():
            return False
        if text.strip().lower() in {"/cancel", "/stop"}:
            p.future.set_exception(Cancelled())
            return True
        if p.kind in ("text", "choice"):
            p.future.set_result(text)
            return True
        return False

    def feed_choice(self, chat_id: int, value: str) -> bool:
        p = self._pending.get(chat_id)
        if not p or p.future.done() or p.kind != "choice":
            return False
        p.future.set_result(value)
        return True

    def feed_photo(self, chat_id: int, file_path: str) -> bool:
        p = self._pending.get(chat_id)
        if not p or p.future.done() or p.kind != "photos":
            return False
        p.photos.append(file_path)
        return True

    def feed_photos_done(self, chat_id: int) -> bool:
        p = self._pending.get(chat_id)
        if not p or p.future.done() or p.kind != "photos":
            return False
        p.future.set_result(list(p.photos))
        return True

    def cancel(self, chat_id: int):
        p = self._pending.get(chat_id)
        if p and not p.future.done():
            p.future.set_exception(Cancelled())

    # ---- called by the wizard -------------------------------------------
    async def ask_text(self, bot: Bot, chat_id: int, prompt: str,
                       timeout: int = 600) -> str:
        await bot.send_message(chat_id, prompt + "\n\n<i>/cancel to stop</i>")
        return await self._await(chat_id, "text", timeout)

    async def ask_choice(self, bot: Bot, chat_id: int, prompt: str,
                         options: list[tuple[str, str]], timeout: int = 600) -> str:
        """options = list of (label, value). Returns the chosen value."""
        rows = [[InlineKeyboardButton(text=label, callback_data=f"wz:{value}")]
                for label, value in options]
        rows.append([InlineKeyboardButton(text="✖️ Cancel", callback_data="wz:__cancel__")])
        await bot.send_message(chat_id, prompt,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return await self._await(chat_id, "choice", timeout)

    async def ask_photos(self, bot: Bot, chat_id: int, prompt: str,
                         timeout: int = 900) -> list[str]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Done", callback_data="wz:__photos_done__"),
            InlineKeyboardButton(text="✖️ Cancel", callback_data="wz:__cancel__"),
        ]])
        await bot.send_message(chat_id, prompt, reply_markup=kb)
        return await self._await(chat_id, "photos", timeout)

    async def _await(self, chat_id: int, kind: str, timeout: int):
        self._pending[chat_id] = _Pending(kind)
        try:
            return await asyncio.wait_for(self._pending[chat_id].future, timeout)
        finally:
            self._pending.pop(chat_id, None)


broker = AskBroker()
