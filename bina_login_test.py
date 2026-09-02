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

LOGIN_URL = os.getenv("LOGIN_URL", "https://bina.az/login")
MY_ITEMS_URL = os.getenv("MY_ITEMS_URL", "https://bina.az/items/my")

# Selectors. Every one can be overridden by an env var of the same name,
# so you can fix them from the workflow inputs without editing this file.
SELECTORS = {
    "phone_input": os.getenv("SEL_PHONE_INPUT", "input[name='phone'], input[type='tel']"),
    "phone_submit": os.getenv("SEL_PHONE_SUBMIT", "button[type='submit']"),
    "otp_input": os.getenv("SEL_OTP_INPUT", "input[name='code'], input[name='otp']"),
    "otp_submit": os.getenv("SEL_OTP_SUBMIT", "button[type='submit']"),
    "otp_error": os.getenv("SEL_OTP_ERROR", ".error, .invalid-feedback, [role='alert']"),
    "logged_in": os.getenv("SEL_LOGGED_IN", "a[href*='logout'], a[href*='/users/'], text=Çıxış"),
    "cookie_accept": os.getenv("SEL_COOKIE_ACCEPT", ""),
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def mask(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    return f"+{d[:4]}***{d[-4:]}" if len(d) > 8 else "+***"


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

        log(f"Opening {LOGIN_URL}")
        resp = await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        title = await page.title()
        log(f"HTTP {status} — title: {title!r}")
        log(f"Final URL: {page.url}")

        body = (await page.content()).lower()
        blocked = any(s in body for s in [
            "cf-challenge", "cloudflare", "just a moment",
            "checking your browser", "captcha", "access denied",
        ])
        if blocked or status in (403, 429, 503):
            log("")
            log("🚨 LOOKS BLOCKED. bina.az served a challenge page instead of")
            log("   the login form. This is the datacenter-IP problem — see")
            log("   the README section 'If the probe says BLOCKED'.")

        log("\nSelector check on the login page:")
        found = await probe_selectors(page, ["phone_input", "phone_submit", "cookie_accept"])
        await dump(page, "probe-login-page")

        (ART / "probe-result.json").write_text(json.dumps({
            "status": status, "title": title, "url": page.url,
            "blocked": blocked, "selectors": found,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        await browser.close()

        ok = (not blocked) and found.get("phone_input", 0) > 0
        log("")
        log("✅ Probe OK — the login form is reachable and the phone input was found."
            if ok else
            "❌ Probe failed — read artifacts/probe-login-page.html to find the right selectors.")
        return 0 if ok else 1


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
                phone = BINA_PHONE
                log(f"Using phone from secret: {mask(phone)}")
                await tg.send(f"Using the phone number from your repo secret: <b>{mask(phone)}</b>")
            else:
                await tg.send("📱 Send me the phone number of your bina.az account\n"
                              "(e.g. <code>+994501234567</code>)")
                phone = await tg.wait_for_text(PHONE_WAIT)
                log(f"Phone received: {mask(phone)}")

            # ---------------- step 2: open login page ----------------
            log(f"Opening {LOGIN_URL}")
            resp = await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            log(f"HTTP {resp.status if resp else '?'} — {page.url}")

            body = (await page.content()).lower()
            if any(s in body for s in ["just a moment", "checking your browser", "cf-challenge"]):
                await dump(page, "blocked")
                await tg.send("🚨 bina.az served a Cloudflare challenge instead of the "
                              "login page. GitHub's IP is being blocked.\n\n"
                              "This is the expected failure mode — see the README.")
                return 2

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
