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
MY_ITEMS_URL = os.getenv("MY_ITEMS_URL", "https://bina.az/profile/items").strip()

HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "sessions"))
DEBUG_DIR = Path(__file__).resolve().parent / "debug"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

SELECTORS = {
    "login_trigger": "button[data-cy='header-profile-btn']",
    "phone_choice": "text=Telefon nömrəsi",
    "phone_input": "#phone-field, input[type='tel'], input[name*='phone'], input[name*='number']",
    "phone_submit": "button:has-text('SMS-kod')",
    # The real code field: <input id="sms-code-field" ...> — no name/type/data-cy.
    "otp_input": "#sms-code-field, input[inputmode='numeric']:not(#phone-field):not([data-cy='phone-input'])",
    # The code page has NO submit button (only 'resend'); it auto-submits, so
    # otp_submit is intentionally empty and we rely on typing + Enter.
    "otp_submit": "",
    # NOTE: bina.az is a Next.js app and renders an EMPTY screen-reader element
    # <p id="__next-route-announcer__" role="alert"> on every page. A bare
    # [role='alert'] match therefore looked like an OTP rejection on success.
    "otp_error": ".error, .invalid-feedback, .message--error, "
                 "[role='alert']:not(#__next-route-announcer__):not([aria-live])",
    # NOTE: kept for reference only — NOT used for login detection. On the
    # logged-in homepage bina.az renders NO /profile, /items/my or logout link
    # (the profile control is an avatar <div role="button"> with no href), so
    # these are unreliable. See is_logged_in() for the correct signal.
    "logged_in": "a[href*='/profile'], a[href*='/items/my'], a[href*='logout']",
}

LOGIN_TRIGGERS = [SELECTORS["login_trigger"], "[data-stat='header-profile-btn']",
                  "button:has-text('Giriş')", "text=Giriş"]
PHONE_CHOICES = [
    "a[data-stat='auth-by-phone']",           # the real modal link (homepage HTML)
    "a[data-cy='auth-btn-default']",
    SELECTORS["phone_choice"],
    "a:has-text('Telefon nömrəsi')",
    "button:has-text('Telefon nömrəsi')",
    "text=Telefon nömrəsi ilə giriş",
]
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
        # Never let a single action hang on Playwright's 30s default — the
        # auth pages navigate away mid-action, and a 30s stall looks like a
        # crash to the user.
        self._ctx.set_default_timeout(15000)
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
                    await loc.click(timeout=timeout)
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
        """Logged-out IFF the header shows the 'Giriş' button.

        Verified against real page snapshots:
          logged OUT -> <button data-cy="header-profile-btn">Giriş</button>
          logged IN  -> that button is ABSENT; it is replaced by an avatar
                        dropdown (<div role="button">) that has NO data-cy and
                        NO href, and the homepage contains no /profile,
                        /items/my or logout link.

        So absence-of-Giriş is the only reliable positive signal. We guard it
        with a "did the header actually render?" check so a blank/failed page
        is never mistaken for a logged-in one.
        """
        try:
            await self._page.goto(HOME_URL, wait_until="domcontentloaded")
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

        # 1) Did the page/header actually render? ('Yeni elan' is always there)
        try:
            rendered = await self._page.locator(
                "a[data-cy='header-add-new-item-btn'], header#header, header"
            ).count()
        except Exception:
            rendered = 0
        if not rendered:
            _log("login check: header did not render -> treating as logged out")
            return False

        # 2) 'Giriş' button present => logged OUT.
        try:
            if await self._page.locator(
                    "button[data-cy='header-profile-btn']").count():
                return False
        except Exception:
            return False

        # 3) Header rendered and no 'Giriş' button => logged IN.
        return True

    async def _open_auth(self) -> bool:
        """Reach the phone-entry field the way a human does:
        homepage -> click 'Giriş' -> click 'Telefon nömrəsi ilə giriş'.

        Going straight to hello.bina.az can bounce back to the homepage, so we
        drive it through the header button and the auth modal instead.
        """
        # 1) homepage
        await self._page.goto(HOME_URL, wait_until="domcontentloaded")
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # bina.az ads/analytics never go idle
        await self._pause()

        # If a phone field is already visible (rare), done.
        if await self._visible(SELECTORS["phone_input"]):
            return True

        # 2) click the header 'Giriş' button to open the auth modal
        await self._click_first(LOGIN_TRIGGERS)
        await self._pause()

        # 3) click 'Telefon nömrəsi ilə giriş' (opens hello.bina.az phone page)
        await self._click_first(PHONE_CHOICES)
        await asyncio.sleep(2.0)
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # bina.az ads/analytics never go idle

        # 4) the phone page may open as a new tab/window — switch to it
        try:
            for pg in list(self._ctx.pages):
                if "hello.bina.az" in (pg.url or "").lower():
                    self._page = pg
                    await self._page.bring_to_front()
                    break
        except Exception:
            pass

        if await self._visible(SELECTORS["phone_input"], timeout=6000):
            return True

        # 5) fallback: navigate to the auth URL directly
        await self._page.goto(AUTH_URL, wait_until="domcontentloaded")
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # bina.az ads/analytics never go idle
        await self._pause()
        return await self._visible(SELECTORS["phone_input"], timeout=6000)

    async def _type_phone(self) -> bool:
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
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # bina.az ads/analytics never go idle
        await self._pause()
        # Wait for the OTP field to actually appear before we ask the user.
        return await self._wait_for_otp_field()

    async def _wait_for_otp_field(self, timeout: float = 25.0) -> bool:
        """Poll for the SMS-code input after the phone is submitted.

        Returns True only when the real code field is present. Stops early and
        returns False if we've navigated AWAY from the auth service (which
        means login auto-completed and no code is needed) — so we never type a
        code into the wrong input (e.g. the homepage search box).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = (self._page.url or "").lower()
            # left the auth service -> logged in, no OTP field will appear
            if "hello.bina.az" not in url and "authentication" not in url:
                return False
            try:
                f = self._page.locator("#sms-code-field").first
                if await f.count() and await f.is_visible():
                    return True
                f2 = self._page.locator(SELECTORS["otp_input"]).first
                if await f2.count() and await f2.is_visible():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        # Not found with known selectors — inventory ALL inputs for debugging
        # and try a smart fallback (any visible text/number input that isn't
        # the phone field).
        await self.snapshot("otp-field-missing")
        try:
            inv = await self._page.evaluate(
                """() => Array.from(document.querySelectorAll('input'))
                     .filter(el => el.getClientRects().length)
                     .map(el => ({
                        name: el.getAttribute('name') || '',
                        type: el.getAttribute('type') || '',
                        cy: el.getAttribute('data-cy') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        inputmode: el.getAttribute('inputmode') || '',
                        maxlength: el.getAttribute('maxlength') || '',
                        cls: el.className || '',
                     }))"""
            )
            _log(f"OTP field not found. Visible inputs on page: {inv}")
        except Exception:
            pass
        return False

    async def _otp_locator(self):
        """Return the SMS-code field locator (strictly #sms-code-field first).

        No broad 'any visible input' fallback — that risked grabbing the
        homepage search box and typing the code there.
        """
        for sel in ("#sms-code-field", SELECTORS["otp_input"]):
            if not sel:
                continue
            loc = self._page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        return self._page.locator("#sms-code-field").first  # will error + snapshot

    async def _submit_otp(self, code: str) -> None:
        field = await self._otp_locator()
        try:
            await field.wait_for(state="visible", timeout=8000)
        except Exception:
            await self.snapshot("otp-field-missing")
            raise LoginError(
                "SMS kod xanası tapılmadı. /debug yazıb otp-field-missing.html "
                "faylını göndərin ki, seçicini düzəldim.")
        await field.click()
        try:
            await field.fill("")
        except Exception:
            pass
        # Type the code. IMPORTANT: bina.az AUTO-SUBMITS as soon as the last
        # digit lands — the field then disappears and login completes. So we
        # must not assume the element still exists after typing.
        try:
            await field.type(code, delay=110)
        except Exception:
            pass                      # it may vanish mid-typing; that's fine
        await asyncio.sleep(1.2)

        # Did the page already move on (auto-submit succeeded)?
        gone = False
        try:
            gone = (await field.count()) == 0 or not await field.is_visible(timeout=1000)
        except Exception:
            gone = True

        if not gone:
            # Still waiting for us -> submit explicitly, with a SHORT timeout so
            # we never hang for the 30s default.
            try:
                if SELECTORS.get("otp_submit"):
                    await self._click_first([SELECTORS["otp_submit"]], timeout=3000)
                else:
                    await field.press("Enter", timeout=5000)
            except Exception:
                pass                  # auto-submit may have fired meanwhile

        try:
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await self._pause()

        # SUCCESS BEATS ERROR-DETECTION. If we've left the auth service, the
        # code was accepted — do not go looking for "errors" on the logged-in
        # page (Next.js renders an empty role=alert announcer everywhere).
        url = (self._page.url or "").lower()
        if "hello.bina.az" not in url and "authentication" not in url:
            return

        # Rejected? Only count it if there is REAL, non-empty error text.
        try:
            err = self._page.locator(SELECTORS["otp_error"]).first
            if await err.count() and await err.is_visible(timeout=2000):
                text = ((await err.inner_text()) or "").strip()
                if text:
                    raise OtpRejected(text[:150])
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

        # Submitting the phone can lead to two outcomes:
        #  (a) the SMS-code field appears  -> we need an OTP, or
        #  (b) bina.az auto-completes login (valid prior session) -> no OTP.
        otp_ready = await self._type_phone()
        if not otp_ready:
            # Maybe it auto-logged-in instead of showing the code field.
            if await self.is_logged_in():
                await self._save()
                return True
            raise LoginError(
                "Nömrə daxil edildi, amma SMS xanası görünmədi. /debug yazıb "
                "otp-field-missing.html faylını göndərin.")

        error: str | None = None
        for attempt in range(1, OTP_MAX_ATTEMPTS + 1):
            code = re.sub(r"\D", "", await otp_provider(self.phone, attempt, error))
            if not code:
                error = "Yalnız rəqəm göndərin."
                continue
            try:
                await self._submit_otp(code)
            except OtpRejected as exc:
                error = f"bina.az kodu qəbul etmədi ({exc})."
                continue
            # Give the auth redirect a moment to land before verifying.
            await asyncio.sleep(2.5)
            if await self.is_logged_in():
                await self._save()
                return True
            error = "Kod qəbul edildi, amma giriş tamamlanmadı."
        raise LoginError(f"Giriş {OTP_MAX_ATTEMPTS} cəhddən sonra alınmadı.")
