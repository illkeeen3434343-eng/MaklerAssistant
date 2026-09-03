"""
bina.az automation core — the logic proven by the login test, packaged for
reuse by the persistent bot.

Key addition over the test: the browser context is kept alive and its
storage_state (cookies) is saved after login, so subsequent runs skip the OTP
entirely until the session expires.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

import security

# --------------------------------------------------------------------------
# Config (env-overridable, same defaults the login test converged on)
# --------------------------------------------------------------------------
RETURN_TO = os.getenv("RETURN_TO", "https://bina.az/").strip() or "https://bina.az/"
_RT_B64 = base64.urlsafe_b64encode(RETURN_TO.encode()).decode().rstrip("=")
HOME_URL = os.getenv("HOME_URL", "https://bina.az/").strip()
AUTH_URL = os.getenv("AUTH_URL", f"https://hello.bina.az/?return_to={_RT_B64}").strip()
MY_ITEMS_URL = os.getenv("MY_ITEMS_URL", "https://bina.az/items/my").strip()

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "sessions"))
DEBUG_DIR = Path("debug")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

SELECTORS = {
    "login_trigger": "button[data-cy='header-profile-btn']",
    "phone_choice": "text=Telefon nömrəsi",
    "phone_input": "#phone-field, input[type='tel'], input[name*='phone'], input[name*='number']",
    "phone_submit": "button:has-text('SMS-kod')",
    "otp_input": "input[name*='code'], input[name*='otp'], input[autocomplete='one-time-code'], input[inputmode='numeric']",
    "otp_submit": "button[type='submit'], button:has-text('Təsdiq'), button:has-text('Daxil')",
    "otp_error": ".error, .invalid-feedback, [role='alert']",
    "logged_in": "a[href*='/profile'], a[href*='/items/my'], a[href*='logout']",
}

LOGIN_TRIGGERS = [SELECTORS["login_trigger"], "[data-stat='header-profile-btn']",
                  "button:has-text('Giriş')", "text=Giriş"]
PHONE_CHOICES = [SELECTORS["phone_choice"], "button:has-text('Telefon nömrəsi')",
                 "a:has-text('Telefon nömrəsi')", "text=Telefon nömrəsi ilə giriş"]
SUBMIT_BUTTONS = ["button:has-text('SMS-kod')", "button[type='submit']",
                  "input[type='submit']", "button:has-text('Davam')",
                  "button:has-text('Daxil ol')", "button:has-text('Təsdiq')",
                  "button:has-text('Göndər')", "button:has-text('İrəli')"]

# The callback the bot supplies to hand us an OTP typed in Telegram.
OtpProvider = Callable[[str, int, "str | None"], Awaitable[str]]
OTP_MAX_ATTEMPTS = 3


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] bina: {msg}", flush=True)


def bina_local(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    if d.startswith("994") and len(d) >= 12:
        d = d[3:]
    if d.startswith("0"):
        d = d[1:]
    return d


def mask(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    return d[:2] + "*" * max(0, len(d) - 4) + d[-2:] if len(d) >= 5 else "***"


# --------------------------------------------------------------------------
class LoginError(Exception):
    pass


class OtpRejected(Exception):
    pass


class BinaSession:
    """One persistent browser context for one (owner_id, phone) pair."""

    def __init__(self, phone: str, owner_id: int):
        self.phone = phone
        self.owner_id = owner_id
        self.local = bina_local(phone)
        safe = re.sub(r"\D", "", phone)
        # Session files are namespaced per owner AND encrypted per owner, so
        # one user can never read or reuse another user's session.
        self.session_file = SESSIONS_DIR / str(owner_id) / f"{safe}.enc"
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self.lock = asyncio.Lock()

    # ---- lifecycle ----
    async def start(self) -> None:
        if self._ctx is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        state = None
        if self.session_file.exists():
            try:
                raw = security.decrypt(self.owner_id, self.session_file.read_bytes())
                if raw:
                    state = json.loads(raw)
                    _log(f"loaded saved session for {mask(self.phone)}")
            except Exception:
                state = None
        self._ctx = await self._browser.new_context(
            storage_state=state, user_agent=USER_AGENT,
            locale="az-AZ", timezone_id="Asia/Baku",
            viewport={"width": 1366, "height": 900},
        )
        self._page = await self._ctx.new_page()

    @property
    def page(self):
        return self._page

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = self._browser = self._pw = self._page = None

    async def _save(self) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        state = await self._ctx.storage_state()
        blob = security.encrypt(self.owner_id, json.dumps(state).encode())
        tmp = self.session_file.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.session_file)
        try:
            os.chmod(self.session_file, 0o600)
        except OSError:
            pass
        _log(f"session saved for {mask(self.phone)} (owner {self.owner_id})")

    def forget(self) -> None:
        if self.session_file.exists():
            self.session_file.unlink()

    # ---- helpers ----
    async def _pause(self) -> None:
        await asyncio.sleep(random.uniform(0.5, 1.4))

    async def _visible(self, sel: str, timeout: int = 2500) -> bool:
        if not sel:
            return False
        try:
            return await self._page.locator(sel).first.is_visible(timeout=timeout)
        except Exception:
            return False

    async def _click_first(self, selectors: list[str], timeout: int = 3500) -> bool:
        for sel in selectors:
            if not sel:
                continue
            try:
                loc = self._page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=timeout):
                    await loc.click()
                    return True
            except Exception:
                continue
        return False

    async def snapshot(self, tag: str) -> None:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stem = DEBUG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}"
        try:
            await self._page.screenshot(path=f"{stem}.png", full_page=True)
            (stem.with_suffix(".html")).write_text(await self._page.content(),
                                                   encoding="utf-8")
        except Exception:
            pass

    # ---- auth ----
    async def is_logged_in(self) -> bool:
        await self._page.goto(MY_ITEMS_URL, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        low = self._page.url.lower()
        bounced = ("login" in low) or ("hello.bina.az" in low) or ("authentication" in low)
        return (not bounced) and "/items/my" in low

    async def _open_auth(self) -> bool:
        await self._page.goto(AUTH_URL, wait_until="domcontentloaded")
        await self._page.wait_for_load_state("networkidle")
        await self._pause()
        if await self._visible(SELECTORS["phone_input"]):
            return True
        await self._click_first(PHONE_CHOICES)
        await self._pause()
        return await self._visible(SELECTORS["phone_input"], timeout=6000)

    async def _type_phone(self) -> None:
        field = self._page.locator(SELECTORS["phone_input"]).first
        await field.wait_for(state="visible", timeout=15000)
        await field.click()
        try:
            await field.press("Control+A")
            await field.press("Backspace")
        except Exception:
            pass
        for _ in range(6):
            try:
                await field.press("Backspace")
            except Exception:
                break
        for ch in self.local:
            await self._page.keyboard.type(ch, delay=random.randint(60, 130))
        await asyncio.sleep(0.4)
        # wait for the SMS button to enable
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                b = self._page.locator(SELECTORS["phone_submit"]).first
                if await b.count():
                    st = await b.evaluate(
                        "el => el.disabled || el.className.includes('disabled')")
                    if not st:
                        break
            except Exception:
                pass
            await asyncio.sleep(0.4)
        if not await self._click_first([SELECTORS["phone_submit"]] + SUBMIT_BUTTONS):
            await field.press("Enter")
        await self._page.wait_for_load_state("networkidle")
        await self._pause()

    async def _submit_otp(self, code: str) -> None:
        field = self._page.locator(SELECTORS["otp_input"]).first
        await field.wait_for(state="visible", timeout=15000)
        await field.click()
        await field.fill("")
        await field.type(code, delay=110)
        await asyncio.sleep(0.6)
        if not await self._click_first([SELECTORS["otp_submit"]] + SUBMIT_BUTTONS):
            await field.press("Enter")
        await self._page.wait_for_load_state("networkidle")
        await self._pause()
        # rejected?
        try:
            err = self._page.locator(SELECTORS["otp_error"]).first
            if await err.is_visible(timeout=2000):
                raise OtpRejected((await err.inner_text())[:150])
        except OtpRejected:
            raise
        except Exception:
            pass

    async def ensure_logged_in(self, otp_provider: OtpProvider) -> bool:
        """Reuse saved session if possible; otherwise run the OTP flow.

        Returns True if a fresh OTP login happened, False if the saved
        session was reused (so the bot can tell the user).
        """
        await self.start()
        if await self.is_logged_in():
            return False

        if not await self._open_auth():
            await self.snapshot("no-auth-form")
            raise LoginError("Could not reach the phone form on hello.bina.az.")

        await self._type_phone()

        error: str | None = None
        for attempt in range(1, OTP_MAX_ATTEMPTS + 1):
            code = re.sub(r"\D", "", await otp_provider(self.phone, attempt, error))
            if not code:
                error = "That wasn't a code — digits only."
                continue
            try:
                await self._submit_otp(code)
            except OtpRejected as exc:
                error = f"bina.az rejected the code ({exc})."
                continue
            if await self.is_logged_in():
                await self._save()
                return True
            error = "Code accepted but session still logged out."
        raise LoginError(f"Login failed after {OTP_MAX_ATTEMPTS} attempts.")
