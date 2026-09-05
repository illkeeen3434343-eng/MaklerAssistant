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
MY_ADS_URL = "https://bina.az/profile/items"

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
    "village_button": "[data-cy='item-form-village']",                 # confirmed (opener, appears after district)
    "address": "input[name='address']",                                # confirmed (Ünvan)
    "search_input": "[data-cy='search-input'], input[type='search']",    # confirmed (inside dropdowns)
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
    "[role='listbox'] [role='button']",
    "[class*='option']",
    "[class*='dropdown'] li",
    "[class*='menu'] li",
    "ul li",
    "li",
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
            await loc.scroll_into_view_if_needed(timeout=8000)
            await loc.wait_for(state="visible", timeout=8000)
            await loc.click()
            await loc.fill(str(value))
            return
        except Exception:
            pass
        # Fallback: set the value via JS and dispatch input/change events so
        # React registers it (some fields are picky about how they're filled).
        try:
            ok = await self.page.evaluate(
                """({sel, val}) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.scrollIntoView({block:'center'});
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                }""",
                {"sel": selector, "val": str(value)},
            )
            if ok:
                return
        except Exception:
            pass
        await self.s.snapshot(f"publish-{what}")
        raise PublishError(f"Could not fill {what} ({selector}).")

    async def _fill_textarea(self, value: str):
        """Description is a textarea wrapped in a role=button container; click
        the wrapper first, then type into the textarea by data-cy."""
        try:
            wrap = self.page.locator("[data-cy='text-area-container']").first
            await wrap.click()
            ta = self.page.locator("[data-cy='text-area-input'], textarea[name='description']").first
            await ta.fill("")
            await ta.type(str(value), delay=15)
        except Exception as exc:
            await self.s.snapshot("publish-description")
            raise PublishError(f"Could not fill description: {exc}") from exc

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

    async def fetch_my_ads(self) -> list[dict]:
        """Read the user's listings from /profile/items.

        Returns a list of dicts: id, title, price, params, status, url.
        Structure from the profile page: each card is [data-cy='item-card'].
        """
        await self.page.goto(MY_ADS_URL, wait_until="domcontentloaded")
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(2.0)
        low = self.page.url.lower()
        if "login" in low or "hello.bina.az" in low:
            raise PublishError("Not logged in — profile redirected to login.")

        ads = await self.page.evaluate(
            """() => {
                const out = [];
                for (const card of document.querySelectorAll("[data-cy='item-card']")) {
                    const link = card.querySelector("a[href*='/items/']");
                    const href = link ? link.getAttribute('href') : '';
                    const m = href.match(/\\/items\\/(\\d+)/);
                    const priceEl = card.querySelector("[data-cy='item-card-price-full']");
                    const titleEl = card.querySelector(".sc-c97f875-16");   // location line
                    const params = Array.from(card.querySelectorAll(".sc-c97f875-17 span"))
                                        .map(s => s.textContent.trim()).filter(Boolean);
                    const statusEl = card.querySelector("[data-cy^='product-label']");
                    out.push({
                        id: m ? m[1] : '',
                        url: href.startsWith('http') ? href : 'https://bina.az' + href,
                        title: titleEl ? titleEl.textContent.trim() : '',
                        price: priceEl ? priceEl.textContent.replace(/\\s/g,' ').trim() : '',
                        params: params.join(', '),
                        status: statusEl ? statusEl.textContent.trim() : '',
                    });
                }
                return out;
            }"""
        )
        return ads

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

    async def _close_overlay(self):
        """Press Escape / click empty space to dismiss an open dropdown overlay
        so the next opener isn't blocked by it (the district-click failure)."""
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
        except Exception:
            pass

    async def search_and_pick(self, opener_key: str, query: str, tag: str) -> list[str]:
        """Open a search-dropdown, type `query`, return the visible result texts.

        These bina.az fields (city, district, village) each contain a
        data-cy='search-input'. Typing filters the list; we then read the
        results so the caller can show them as buttons and pick one.
        """
        await self._close_overlay()
        await self._click(PUB[opener_key], f"open-{tag}")
        await asyncio.sleep(0.8)

        # find the search box that just became active and type into it
        typed = False
        try:
            box = self.page.locator(PUB["search_input"]).last
            await box.wait_for(state="visible", timeout=4000)
            await box.click()
            await box.fill("")
            if query:
                await box.type(query, delay=60)
                typed = True
            await asyncio.sleep(1.0)
        except Exception:
            pass

        before_skip = set(self._NON_OPTIONS)
        results = []
        for _ in range(10):
            await asyncio.sleep(0.4)
            results = [t for t in await self._scan_option_texts()
                       if t not in before_skip]
            if results:
                break
        await self.s.snapshot(f"publish-{tag}-open")
        return results

    async def pick_result(self, text: str, tag: str):
        await self._click_text(text, tag)
        await asyncio.sleep(1.0)

    async def open_map_and_confirm(self):
        """Click 'Xəritədə göstər' and confirm the map popup (#4).

        Selectors are best-effort; the map confirm button text varies. Failures
        are non-fatal — the address text field already sets the location.
        """
        try:
            btn = self.page.locator("[data-cy='new-ad-select-on-map']").first
            if await btn.count() and await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(2.0)
                # try common confirm buttons in the map popup
                for sel in ["button:has-text('Təsdiq')", "button:has-text('OK')",
                            "button:has-text('Seç')", "button:has-text('Hazır')",
                            "[data-cy*='confirm']", "button[type='submit']"]:
                    try:
                        c = self.page.locator(sel).first
                        if await c.count() and await c.is_visible(timeout=1500):
                            await c.click()
                            await asyncio.sleep(1.0)
                            return True
                    except Exception:
                        continue
                await self.s.snapshot("publish-map-open")
        except Exception:
            pass
        return False

    async def discover_options(self, opener_key: str, filter_text: str | None = None,
                               tag: str = "dropdown") -> list[str]:
        """Open a dropdown and return the visible option texts.

        The form has permanent <ul><li> TAB lists (Satıram/Kirayə,
        Elanın sahibi/Mən vasitəçiyəm) that must NOT be treated as options.
        We snapshot the visible option-texts BEFORE opening, then again AFTER,
        and return only the newly-appeared ones — so tabs and other static
        chrome are excluded automatically.
        """
        # texts visible before opening (tabs, labels, etc.)
        before = set(await self._scan_option_texts())

        await self._click(PUB[opener_key], f"open-{tag}")

        if filter_text:
            try:
                si = self.page.locator(PUB["search_input"]).last
                if await si.is_visible(timeout=2000):
                    await si.fill(filter_text)
                    await asyncio.sleep(1.0)
            except Exception:
                pass

        # Poll up to ~6s for the option overlay to render (React portal).
        new: list[str] = []
        for _ in range(12):
            await asyncio.sleep(0.5)
            after = await self._scan_option_texts()
            new = [t for t in after if t not in before]
            if len(new) >= 2:            # a real list appeared
                break

        await self.s.snapshot(f"publish-{tag}-open")
        return new or [t for t in (await self._scan_option_texts()) if t not in before]

    # Known tab/label texts that are never real dropdown options.
    _NON_OPTIONS = {
        "Satıram", "Kirayə verirəm", "Elanın sahibi", "Mən vasitəçiyəm",
        "Təmirli", "Təmirsiz", "Çıxarış var", "İpoteka var",
        "Xəritədə göstər", "Şəkil əlavə etmək",
    }

    async def _scan_option_texts(self) -> list[str]:
        return await self.page.evaluate(
            """(candidates) => {
                const skip = %s;
                const seen = new Set(); const out = [];
                for (const sel of candidates) {
                    let els; try { els = document.querySelectorAll(sel); } catch { continue; }
                    for (const el of els) {
                        if (!el.getClientRects().length) continue;      // visible only
                        // skip the permanent tab bars
                        if (el.closest("[role='tab']") || el.getAttribute('role') === 'tab') continue;
                        const t = (el.innerText || '').trim();
                        if (!t || t.length > 60 || seen.has(t)) continue;
                        if (skip.includes(t)) continue;
                        seen.add(t); out.push(t);
                    }
                }
                return out.slice(0, 80);
            }""".replace("%s", "[" + ",".join(f'"{x}"' for x in self._NON_OPTIONS) + "]"),
            OPTION_CANDIDATES,
        )

    async def pick_option(self, text: str, tag: str = "option"):
        await self._click_text(text, tag)
        await asyncio.sleep(1.0)

    async def fill_details(self, *, rooms=None, area=None, floor=None,
                           total_floors=None, description=None, price=None,
                           address=None):
        if address is not None:
            await self._fill(PUB["address"], address, "address")
        if rooms is not None:
            await self._fill(PUB["rooms"], rooms, "rooms")
        if area is not None:
            await self._fill(PUB["area"], area, "area")
        if floor is not None:
            await self._fill(PUB["floor"], floor, "floor")
        if total_floors is not None:
            await self._fill(PUB["total_floors"], total_floors, "total-floors")
        if description is not None:
            await self._fill_textarea(description)
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
