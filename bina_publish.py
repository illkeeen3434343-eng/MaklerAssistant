"""
bina.az "new listing" (Yeni elan) automation.

Drives the multi-step publish wizard on a logged-in page. Selectors marked
(confirmed) came from real page HTML you captured. Selectors marked (guess)
are for the dropdown OPTION lists, which render dynamically and haven't been
captured yet — discover_options() also scrapes them live and dump()s the HTML
so they can be pinned from one probe run.

Scope: apartment-type categories ("Yeni tikili" / "Köhnə tikili"), which share
the same form fields. Other categories (land, garage…) have different fields
and aren't handled yet.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout

# --------------------------------------------------------------------------
NEW_AD_URL = "https://bina.az/items/new"

PUB = {
    # entry
    "new_ad_button": "a[data-cy='header-add-new-item-btn']",              # confirmed
    # step 1 — deal type
    "deal_sell": "[data-stat='create_ad_step1_sell']",                   # confirmed
    "deal_rent": "[data-stat='create_ad_step1_rent']",                   # confirmed
    # step 2 — category (Yeni tikili / Köhnə tikili have no data-stat → by text)
    "category_option": "[data-cy='category_modal--option']",             # confirmed
    # step 3 — owner vs agent
    "owner_self": "[data-stat='create_ad_step3_owner']",                 # confirmed
    "owner_agent": "[data-stat='create_ad_step3_agent']",                # confirmed
    # step 4 — the big form
    "type_dropdown": "[data-cy='category-dropdown']",                    # confirmed (opener)
    "city_button": "[data-cy='item-form-city']",                        # confirmed (opener)
    "district_button": "[data-cy='item-form-location']",               # confirmed (opener)
    "search_input": "[data-cy='search-input'], input[type='search']",    # confirmed (inside city)
    "rooms": "input[name='roomsAmount']",                               # confirmed
    "area": "input[name='area']",                                      # confirmed
    "floor": "input[name='floor']",                                    # confirmed
    "total_floors": "input[name='totalFloors']",                       # confirmed
    "repair_yes": "[data-cy='new-ad-form-has-repair']",                # confirmed
    "repair_no": "[data-cy='new-ad-form-no-repair']",                  # confirmed
    "photo_input": "[data-cy='item-modal-photo-upload'] input[type='file']",  # confirmed
    "description": "textarea[name='description']",                      # confirmed
    "price": "input[name='price']",                                    # confirmed
    "bill_of_sale": "input[name='hasBillOfSale']",                     # confirmed
    "mortgage": "input[name='hasMortgage']",                           # confirmed
    "contact_owner_tab": "#quick-links-tab-new-ad-form-owner",         # confirmed
    "contact_agent_tab": "#quick-links-tab-new-ad-form-agent",         # confirmed
    "name": "input[name='name']",                                     # confirmed
    "email": "input[name='email']",                                   # confirmed
    "submit": "button[data-cy='new-ad-form-submit-button']",           # confirmed ("Davam etmək")
}

# Candidate selectors for OPTION items inside an opened dropdown. These are
# best-effort guesses; discover_options() also scrapes visible text and dumps
# the HTML so the exact selector can be pinned.  (guess)
OPTION_CANDIDATES = [
    "[role='option']",
    "[data-cy*='option']",
    "[data-cy*='dropdown'] li",
    "[role='listbox'] li",
    "ul li",
]


class PublishError(Exception):
    pass


class PublishFlow:
    """Operates on a BinaSession's live, logged-in page."""

    def __init__(self, session):
        self.s = session
        self.page = session.page

    # -------------------------------------------------------------- helpers
    async def _click(self, selector: str, what: str, timeout: int = 8000):
        try:
            loc = self.page.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
        except PWTimeout as exc:
            await self.s.snapshot(f"publish-{what}")
            raise PublishError(f"Could not click {what} ({selector}).") from exc

    async def _fill(self, selector: str, value: str, what: str):
        try:
            loc = self.page.locator(selector).first
            await loc.wait_for(state="visible", timeout=8000)
            await loc.click()
            await loc.fill("")
            await loc.type(str(value), delay=40)
        except PWTimeout as exc:
            await self.s.snapshot(f"publish-{what}")
            raise PublishError(f"Could not fill {what} ({selector}).") from exc

    async def _click_text(self, text: str, what: str):
        """Click an element by its exact visible text (for options/categories)."""
        try:
            await self.page.get_by_text(text, exact=True).first.click(timeout=6000)
        except Exception as exc:
            await self.s.snapshot(f"publish-{what}")
            raise PublishError(f"Could not click '{text}' for {what}.") from exc

    # -------------------------------------------------------------- steps
    async def open_new_ad(self):
        await self.page.goto(NEW_AD_URL, wait_until="domcontentloaded")
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.5)
        low = self.page.url.lower()
        if "login" in low or "hello.bina.az" in low:
            raise PublishError("Not logged in — the new-ad page redirected to login.")

    async def choose_deal(self, sell: bool):
        await self._click(PUB["deal_sell"] if sell else PUB["deal_rent"],
                          "deal-type")
        await asyncio.sleep(1.0)

    async def choose_category(self, category_text: str):
        # e.g. "Yeni tikili" or "Köhnə tikili"
        await self._click_text(category_text, "category")
        await asyncio.sleep(1.0)

    async def choose_owner(self, is_owner: bool):
        await self._click(PUB["owner_self"] if is_owner else PUB["owner_agent"],
                          "owner-type")
        await asyncio.sleep(1.2)

    async def discover_options(self, opener_key: str, filter_text: str | None = None,
                               tag: str = "dropdown") -> list[str]:
        """Open a dropdown and return the visible option texts.

        Also dumps the opened HTML so the exact option selector can be pinned.
        """
        await self._click(PUB[opener_key], f"open-{tag}")
        await asyncio.sleep(1.0)

        if filter_text:
            try:
                si = self.page.locator(PUB["search_input"]).last
                if await si.is_visible(timeout=2000):
                    await si.fill(filter_text)
                    await asyncio.sleep(1.0)
            except Exception:
                pass

        await self.s.snapshot(f"publish-{tag}-open")

        texts = await self.page.evaluate(
            """(candidates) => {
                const seen = new Set(); const out = [];
                for (const sel of candidates) {
                    let els;
                    try { els = document.querySelectorAll(sel); } catch { continue; }
                    for (const el of els) {
                        if (!el.getClientRects().length) continue;      // visible only
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 60 || seen.has(t)) continue;
                        seen.add(t); out.push(t);
                    }
                }
                return out.slice(0, 60);
            }""",
            OPTION_CANDIDATES,
        )
        return texts

    async def pick_option(self, text: str, tag: str = "option"):
        await self._click_text(text, tag)
        await asyncio.sleep(1.0)

    async def fill_details(self, *, rooms=None, area=None, floor=None,
                           total_floors=None, description=None, price=None):
        if rooms is not None:
            await self._fill(PUB["rooms"], rooms, "rooms")
        if area is not None:
            await self._fill(PUB["area"], area, "area")
        if floor is not None:
            await self._fill(PUB["floor"], floor, "floor")
        if total_floors is not None:
            await self._fill(PUB["total_floors"], total_floors, "total-floors")
        if description is not None:
            await self._fill(PUB["description"], description, "description")
        if price is not None:
            await self._fill(PUB["price"], price, "price")

    async def set_repair(self, has_repair: bool):
        await self._click(PUB["repair_yes"] if has_repair else PUB["repair_no"],
                          "repair")
        await asyncio.sleep(0.4)

    async def set_checkbox(self, key: str, on: bool):
        try:
            box = self.page.locator(PUB[key]).first
            checked = await box.is_checked()
            if checked != on:
                await box.click()
        except Exception:
            pass

    async def add_photos(self, paths: list[str]):
        try:
            inp = self.page.locator(PUB["photo_input"]).first
            await inp.set_input_files(paths)
            await asyncio.sleep(2.0)
        except Exception as exc:
            await self.s.snapshot("publish-photos")
            raise PublishError(f"Photo upload failed: {exc}") from exc

    async def fill_contact(self, *, name=None, email=None, is_owner=True):
        # contact tab
        try:
            await self._click(
                PUB["contact_owner_tab"] if is_owner else PUB["contact_agent_tab"],
                "contact-tab", timeout=3000)
        except Exception:
            pass
        if name is not None:
            await self._fill(PUB["name"], name, "name")
        if email is not None:
            await self._fill(PUB["email"], email, "email")

    async def submit(self) -> str:
        """Click 'Davam etmək'. Returns the URL we land on afterwards.

        NOTE: this is 'Continue', not necessarily the final publish — there is
        very likely a package/preview step after it. The caller should inspect
        the returned URL / page.
        """
        await self._click(PUB["submit"], "submit")
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(2.0)
        await self.s.snapshot("publish-after-continue")
        return self.page.url
