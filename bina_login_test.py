"""
bina.az login test — designed to run inside GitHub Actions.

Two modes:

  probe   Loads the bina.az login page, saves the HTML + a screenshot as
          artifacts, and reports which selectors matched. No Telegram, no
          login. Run this FIRST to find the correct selectors.

  login   The real test. Messages you on Telegram, asks for your phone,
          submits it to bina.az, waits for you to send the SMS code, submits
          that, and reports whether the session is authenticated.

The script is deliberately linear — read it top to bottom and you can follow
exactly what happens. No framework, no dispatcher, no state machine.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODE = os.getenv("MODE", "probe").strip().lower()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID", "").strip()
BINA_PHONE = os.getenv("BINA_PHONE", "").strip()   # optional; else asked in chat

PHONE_WAIT = int(os.getenv("PHONE_WAIT", "180"))   # seconds to wait for phone
OTP_WAIT = int(os.getenv("OTP_WAIT", "300"))       # seconds to wait for the code
SAVE_SUCCESS_SCREENSHOT = os.getenv("SAVE_SUCCESS_SCREENSHOT", "false").lower() == "true"

ART = Path("artifacts")
ART.mkdir(exist_ok=True)

def env_or(name: str, default: str) -> str:
    """Like os.getenv, but an EMPTY value also falls back to the default.

    GitHub Actions passes an unset workflow input as an empty string, not as
    an absent variable. os.getenv only uses its default when the variable is
    absent, so a blank override box would otherwise wipe out the fallback and
    hand Playwright an empty '' selector.
    """
    val = os.getenv(name, "")
    return val.strip() if val.strip() else default


# bina.az login is a MODAL opened by the #authentication hash, not a page.
# /login returns a 404. Loading the homepage with the hash pops the modal.
HOME_URL = env_or("HOME_URL", "https://bina.az/")
LOGIN_URL = env_or("LOGIN_URL", "https://bina.az/#authentication")
MY_ITEMS_URL = env_or("MY_ITEMS_URL", "https://bina.az/items/my")

# Selectors. Every one can be overridden by an env var of the same name,
# so you can fix them from the workflow inputs without editing this file.
# A blank override falls back to the default here (see env_or above).
#
# The flow is: open modal -> click "phone number" choice -> enter phone
# (local, no leading 0) -> submit -> enter OTP -> submit.
SELECTORS = {
    # Opens the auth modal if the hash alone didn't. Broad, best-effort.
    "login_trigger": env_or("SEL_LOGIN_TRIGGER",
                            "a[href*='authentication'], [class*='login'], text=Giriş"),
    # The "log in with phone number" button inside the modal.
    "phone_choice": env_or("SEL_PHONE_CHOICE", "text=Telefon nömrəsi ilə giriş"),
    "phone_input": env_or("SEL_PHONE_INPUT",
                          "input[type='tel'], input[name*='phone'], input[name='login']"),
    "phone_submit": env_or("SEL_PHONE_SUBMIT", "button[type='submit']"),
    "otp_input": env_or("SEL_OTP_INPUT",
                        "input[name*='code'], input[name*='otp'], input[autocomplete='one-time-code']"),
    "otp_submit": env_or("SEL_OTP_SUBMIT", "button[type='submit']"),
    "otp_error": env_or("SEL_OTP_ERROR", ".error, .invalid-feedback, [role='alert']"),
    "logged_in": env_or("SEL_LOGGED_IN", "a[href*='logout'], a[href*='/users/'], text=Çıxış"),
    # cookie_accept has no default — empty means "no banner", which is valid.
    "cookie_accept": os.getenv("SEL_COOKIE_ACCEPT", "").strip(),
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def bina_local(phone: str) -> str:
    """Reduce any phone form to the 9-digit local number bina.az expects.

    bina.az's field wants the number WITHOUT the leading 0 and without a
    country code:  0557778899  ->  557778899  ->  as typed.
    Accepts +994557778899, 994557778899, 0557778899, or 557778899.
    """
    d = re.sub(r"\D", "", phone)
    if d.startswith("994") and len(d) >= 12:
        d = d[3:]
    if d.startswith("0"):
        d = d[1:]
    return d


def mask(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    if len(d) >= 5:
        return d[:2] + "*" * (len(d) - 4) + d[-2:]
    return "***"


# ---------------------------------------------------------------------------
# Minimal Telegram client (getUpdates long-polling, no framework)
# ---------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.chat_id = chat_id
        self.offset = 0
        self.http = httpx.AsyncClient(timeout=70)

    async def close(self) -> None:
        await self.http.aclose()

    async def send(self, text: str) -> None:
        try:
            await self.http.post(
                f"{API}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        except Exception as exc:
            log(f"WARN could not send Telegram message: {exc}")

    async def send_photo(self, path: Path, caption: str = "") -> None:
        try:
            with open(path, "rb") as fh:
                await self.http.post(
                    f"{API}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1000]},
                    files={"photo": (path.name, fh, "image/png")},
                )
        except Exception as exc:
            log(f"WARN could not send screenshot: {exc}")

    async def drain(self) -> None:
        """Skip messages sent before this run started."""
        r = await self.http.get(f"{API}/getUpdates", params={"timeout": 0})
        data = r.json()
        if data.get("ok") and data["result"]:
            self.offset = data["result"][-1]["update_id"] + 1
        log(f"Telegram drained, offset={self.offset}")

    async def wait_for_text(self, timeout: int) -> str:
        """Block until the whitelisted user sends a text message."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            try:
                r = await self.http.get(
                    f"{API}/getUpdates",
                    params={"offset": self.offset, "timeout": min(30, max(1, remaining))},
                )
                data = r.json()
            except Exception as exc:
                log(f"getUpdates error: {exc}")
                await asyncio.sleep(2)
                continue

            if not data.get("ok"):
                log(f"Telegram API error: {data}")
                await asyncio.sleep(2)
                continue

            for upd in data["result"]:
                self.offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                sender = str((msg.get("from") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if sender != self.chat_id:
                    log(f"Ignoring message from unauthorised user {sender}")
                    continue
                if text.lower() in {"/cancel", "/stop"}:
                    raise KeyboardInterrupt("cancelled by user")
                # Try to delete so the OTP does not linger in chat history
                try:
                    await self.http.post(
                        f"{API}/deleteMessage",
                        json={"chat_id": self.chat_id,
                              "message_id": msg["message_id"]},
                    )
                except Exception:
                    pass
                return text
        raise TimeoutError(f"No reply within {timeout}s")


# ---------------------------------------------------------------------------
# Selector probing
# ---------------------------------------------------------------------------
async def probe_selectors(page, keys: list[str]) -> dict:
    found = {}
    for key in keys:
        sel = SELECTORS.get(key) or ""
        if not sel:
            found[key] = -1
            log(f"  ⚪️ {key:14} not set")
            continue
        try:
            n = await page.locator(sel).count()
        except Exception as exc:
            found[key] = -2
            log(f"  ❌ {key:14} bad selector: {exc}")
            continue
        found[key] = n
        log(f"  {'✅' if n else '❌'} {key:14} {n} match(es)   [{sel}]")
    return found


async def dump(page, name: str) -> Path:
    html = ART / f"{name}.html"
    png = ART / f"{name}.png"
    html.write_text(await page.content(), encoding="utf-8")
    try:
        await page.screenshot(path=str(png), full_page=True)
    except Exception:
        pass
    log(f"  💾 saved {html} and {png}")
    return png


async def _visible(page, sel: str, timeout: int = 2500) -> bool:
    if not sel:
        return False
    try:
        return await page.locator(sel).first.is_visible(timeout=timeout)
    except Exception:
        return False


async def looks_blocked(page) -> bool:
    body = (await page.content()).lower()
    return any(s in body for s in [
        "cf-challenge", "just a moment", "checking your browser",
        "captcha", "access denied", "attention required",
    ])


async def open_auth_modal(page) -> bool:
    """Open the #authentication modal and select phone-number login.

    Returns True once a phone input is visible. bina.az's login is a modal,
    not a page: loading the homepage with the #authentication hash usually
    pops it. If not, we click a login trigger. Then we click the
    'phone number' choice so the phone field appears.
    """
    log(f"Opening {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)

    if await looks_blocked(page):
        return False

    # If the phone input is already right there, we're done.
    if await _visible(page, SELECTORS["phone_input"]):
        log("Phone input already visible")
        return True

    # Otherwise make sure the modal is open.
    if not await _visible(page, SELECTORS["phone_choice"]):
        log("Modal not open from hash — trying the login trigger")
        try:
            trg = page.locator(SELECTORS["login_trigger"]).first
            if await trg.count() and await trg.is_visible():
                await trg.click()
                await asyncio.sleep(1.5)
        except Exception as exc:
            log(f"  login trigger click failed: {exc}")

    await dump(page, "modal-opened")

    # Click the 'phone number' choice if the phone field isn't showing yet.
    if not await _visible(page, SELECTORS["phone_input"]):
        log("Selecting the phone-number login option")
        try:
            await page.locator(SELECTORS["phone_choice"]).first.click(timeout=5000)
            await asyncio.sleep(1.5)
        except Exception as exc:
            log(f"  phone-choice click failed: {exc}")

    return await _visible(page, SELECTORS["phone_input"], timeout=5000)


# ---------------------------------------------------------------------------
# Mode: probe
# ---------------------------------------------------------------------------
async def run_probe() -> int:
    log("MODE=probe — no login will be attempted")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT, locale="az-AZ", timezone_id="Asia/Baku",
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()

        log(f"Opening homepage {HOME_URL}")
        resp = await page.goto(HOME_URL, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        log(f"HTTP {status} — title: {(await page.title())!r}")

        if await looks_blocked(page) or status in (403, 429, 503):
            log("")
            log("🚨 LOOKS BLOCKED. bina.az served a challenge instead of the page.")
            log("   See the README section 'If the probe says BLOCKED'.")
            await dump(page, "probe-homepage")
            await browser.close()
            return 1

        # Open the auth modal and select phone login.
        opened = await open_auth_modal(page)
        await dump(page, "probe-modal")

        log("\nSelector check inside the login modal:")
        found = await probe_selectors(
            page,
            ["login_trigger", "phone_choice", "phone_input", "phone_submit"],
        )

        # Extract every input/button in the modal so you can read the real
        # attributes straight from the log, without opening the HTML file.
        log("\nInputs and buttons currently on the page:")
        elements = await page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('input, button, [type=submit]')) {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    out.push({
                        tag: el.tagName,
                        type: el.getAttribute('type') || '',
                        name: el.getAttribute('name') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        text: (el.textContent || '').trim().slice(0, 30),
                    });
                }
                return out;
            }"""
        )
        for e in elements[:40]:
            desc = f"<{e['tag']}"
            if e["type"]:
                desc += f" type={e['type']}"
            if e["name"]:
                desc += f" name={e['name']}"
            if e["placeholder"]:
                desc += f" placeholder={e['placeholder']!r}"
            desc += ">"
            if e["text"]:
                desc += f"  “{e['text']}”"
            log(f"  {desc}")

        (ART / "probe-result.json").write_text(json.dumps({
            "homepage_status": status,
            "modal_opened": opened,
            "selectors": found,
            "visible_elements": elements,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        await browser.close()

        log("")
        ok = opened and found.get("phone_input", 0) > 0
        if ok:
            log("=========================================")
            log("  ✅  LOGIN MODAL REACHED, phone input found")
            log("=========================================")
            log("  The datacenter IP is NOT blocked. Run mode=login next.")
            return 0
        log("❌ Could not reach a phone input in the modal.")
        log("   Open artifacts/probe-modal.html (and probe-modal.png) and use the")
        log("   'Inputs and buttons' list above to set the right selector overrides.")
        return 1


# ---------------------------------------------------------------------------
# Mode: login
# ---------------------------------------------------------------------------
async def run_login() -> int:
    tg = Telegram(BOT_TOKEN, TELEGRAM_USER_ID)
    await tg.drain()
    await tg.send(
        "🤖 <b>bina.az login test started</b>\n"
        "Running on GitHub Actions.\n\n"
        "<i>Send /cancel at any point to abort.</i>"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT, locale="az-AZ", timezone_id="Asia/Baku",
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()

        try:
            # ---------------- step 1: phone number ----------------
            if BINA_PHONE:
                raw_phone = BINA_PHONE
                log(f"Using phone from secret: {mask(raw_phone)}")
                await tg.send(f"Using the phone number from your repo secret: "
                              f"<b>{mask(raw_phone)}</b>")
            else:
                await tg.send(
                    "📱 Send me your bina.az phone number.\n"
                    "Just the number as you type it on the site, e.g. "
                    "<code>0557778899</code> or <code>557778899</code>."
                )
                raw_phone = await tg.wait_for_text(PHONE_WAIT)
                log(f"Phone received: {mask(raw_phone)}")

            phone = bina_local(raw_phone)   # -> 9-digit local, no leading 0
            log(f"Normalized to bina.az local form: {mask(phone)} ({len(phone)} digits)")
            if len(phone) != 9:
                await tg.send(
                    f"⚠️ After normalizing I got <b>{len(phone)}</b> digits "
                    f"(<code>{mask(phone)}</code>), but bina.az expects 9 "
                    "(like <code>557778899</code>). Continuing anyway — if it "
                    "fails, resend the number."
                )

            # ---------------- step 2: open modal, choose phone ----------------
            opened = await open_auth_modal(page)
            if await looks_blocked(page):
                await dump(page, "blocked")
                await tg.send("🚨 bina.az served a Cloudflare challenge instead of "
                              "the login modal. GitHub's IP is being blocked.")
                return 2
            if not opened:
                await dump(page, "no-modal")
                await tg.send(
                    "❌ Opened the page but couldn't get a phone input in the "
                    "login modal.\n"
                    f"Phone-choice selector: <code>{SELECTORS['phone_choice']}</code>\n"
                    f"Phone-input selector: <code>{SELECTORS['phone_input']}</code>\n\n"
                    "Run <b>probe</b> mode — it lists the real inputs/buttons — "
                    "then set the selector overrides and retry."
                )
                return 3
            log(f"Login modal ready at {page.url}")

            if SELECTORS["cookie_accept"]:
                try:
                    btn = page.locator(SELECTORS["cookie_accept"]).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        log("Cookie banner dismissed")
                except Exception:
                    pass

            # ---------------- step 3: submit phone ----------------
            log("Filling phone number")
            field = page.locator(SELECTORS["phone_input"]).first
            try:
                await field.wait_for(state="visible", timeout=15000)
            except PWTimeout:
                await dump(page, "no-phone-input")
                await tg.send("❌ Could not find the phone input on the login page.\n"
                              f"Selector tried: <code>{SELECTORS['phone_input']}</code>\n\n"
                              "Download the <b>artifacts</b> from the Actions run and "
                              "look at <code>no-phone-input.html</code>.")
                return 3

            await field.click()
            await field.fill("")
            await field.type(phone, delay=90)
            await asyncio.sleep(1)

            log("Submitting phone number")
            await page.locator(SELECTORS["phone_submit"]).first.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            log(f"After submit, URL: {page.url}")
            await dump(page, "after-phone-submit")

            # ---------------- step 4: wait for OTP ----------------
            otp_field = page.locator(SELECTORS["otp_input"]).first
            try:
                await otp_field.wait_for(state="visible", timeout=15000)
                log("OTP field is present")
            except PWTimeout:
                await dump(page, "no-otp-field")
                await tg.send("❌ Submitted the phone number but no OTP field appeared.\n"
                              f"Selector tried: <code>{SELECTORS['otp_input']}</code>\n\n"
                              "Check <code>after-phone-submit.html</code> in the artifacts — "
                              "bina.az may have rejected the request or the selector is wrong.")
                return 4

            await tg.send(f"📲 bina.az should have texted a code to <b>{mask(phone)}</b>.\n\n"
                          "Send me the code.")
            code = await tg.wait_for_text(OTP_WAIT)
            code = re.sub(r"\D", "", code)
            log(f"Code received ({len(code)} digits)")
            if not code:
                await tg.send("❌ That contained no digits. Aborting.")
                return 5

            # ---------------- step 5: submit OTP ----------------
            await otp_field.click()
            await otp_field.fill("")
            await otp_field.type(code, delay=110)
            await asyncio.sleep(1)
            try:
                await page.locator(SELECTORS["otp_submit"]).first.click(timeout=5000)
            except Exception:
                log("No submit button clicked — form may auto-submit")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            log(f"After OTP, URL: {page.url}")

            # ---------------- step 6: verify ----------------
            log("Verifying session")
            await page.goto(MY_ITEMS_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            final_url = page.url
            log(f"My-items URL resolved to: {final_url}")

            marker_count = 0
            try:
                marker_count = await page.locator(SELECTORS["logged_in"]).count()
            except Exception:
                pass

            redirected_to_login = "login" in final_url.lower()
            success = (not redirected_to_login) and marker_count > 0

            png = await dump(page, "final-state")

            if success:
                log("")
                log("=========================================")
                log("  ✅  LOGIN SUCCESSFUL")
                log("=========================================")
                await tg.send(
                    "✅ <b>Login successful!</b>\n\n"
                    f"Account: <b>{mask(phone)}</b>\n"
                    f"Landed on: <code>{final_url}</code>\n"
                    f"Logged-in marker: {marker_count} match(es)\n\n"
                    "The full flow works: phone → SMS → code → authenticated session.\n\n"
                    "⚠️ <i>This session is destroyed when the Actions job ends. "
                    "Nothing is saved.</i>"
                )
                if SAVE_SUCCESS_SCREENSHOT and png.exists():
                    await tg.send_photo(png, "Logged-in page")
                return 0

            # failure path
            err_text = ""
            try:
                err = page.locator(SELECTORS["otp_error"]).first
                if await err.is_visible(timeout=2000):
                    err_text = (await err.inner_text())[:200]
            except Exception:
                pass

            log("")
            log("  ❌  LOGIN FAILED")
            await tg.send(
                "❌ <b>Login did not complete.</b>\n\n"
                f"Final URL: <code>{final_url}</code>\n"
                f"Redirected to login: {redirected_to_login}\n"
                f"Logged-in marker: {marker_count} match(es)\n"
                + (f"Page error: <i>{err_text}</i>\n" if err_text else "") +
                "\nDownload the run artifacts and open <code>final-state.html</code>.\n"
                "Most likely: the code was wrong, or <code>SEL_LOGGED_IN</code> "
                "does not match anything on the real page."
            )
            return 6

        except KeyboardInterrupt:
            log("Cancelled by user")
            await tg.send("🛑 Cancelled.")
            return 130
        except TimeoutError as exc:
            log(f"Timeout: {exc}")
            await tg.send(f"⏰ {exc}\nThe test has stopped.")
            return 7
        except Exception as exc:
            log(f"Unexpected error: {exc!r}")
            try:
                await dump(page, "crash")
            except Exception:
                pass
            await tg.send(f"💥 Unexpected error:\n<code>{str(exc)[:400]}</code>")
            return 8
        finally:
            await browser.close()
            await tg.close()


# ---------------------------------------------------------------------------
def preflight() -> None:
    problems = []
    if MODE not in {"probe", "login"}:
        problems.append(f"MODE must be 'probe' or 'login', got {MODE!r}")
    if MODE == "login":
        if not BOT_TOKEN:
            problems.append("BOT_TOKEN secret is missing")
        if not TELEGRAM_USER_ID:
            problems.append("TELEGRAM_USER_ID secret is missing")
        elif not TELEGRAM_USER_ID.isdigit():
            problems.append("TELEGRAM_USER_ID must be numeric (get it from @userinfobot)")
    # Guard against an empty required selector reaching Playwright, which
    # produces the cryptic 'unexpected token "" while parsing selector ""'.
    for key in ("phone_choice", "phone_input", "phone_submit", "otp_input", "logged_in"):
        if not SELECTORS.get(key):
            problems.append(f"selector '{key}' is empty — leave its override blank to use the default")
    if problems:
        print("\n❌ Cannot start:\n")
        for p in problems:
            print(f"   • {p}")
        print()
        sys.exit(1)


async def main() -> int:
    preflight()
    log(f"Mode: {MODE}")
    return await (run_probe() if MODE == "probe" else run_login())


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
