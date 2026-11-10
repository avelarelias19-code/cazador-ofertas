"""Scraper Playwright para Amazon México con verificación GLUX por código postal."""
from __future__ import annotations

import re
from typing import Sequence

from playwright.async_api import BrowserContext, Page

from ofertas_monitor.models import Availability, ProductSnapshot, Seller, Store
from ofertas_monitor.seller_filter import is_official_seller
from ofertas_monitor.stores.base import BlockedError, ExtractionError, StoreScraper, parse_price
from ofertas_monitor.stores.selectors import AMAZON_SELECTORS

_ASIN_URL_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)


class AmazonScraper(StoreScraper):
    store = Store.AMAZON

    async def _check_blocked(self, page: Page) -> None:
        for selector in AMAZON_SELECTORS.captcha_markers:
            if await page.locator(selector).count() > 0:
                raise BlockedError("Amazon presentó página de CAPTCHA (respetada sin bypass).")

    async def _first_text(self, page: Page, selector: str) -> str | None:
        loc = page.locator(selector)
        if await loc.count() == 0:
            return None
        return (await loc.first.inner_text()).strip()

    async def _first_attr(self, page: Page, selector: str, attr: str) -> str | None:
        loc = page.locator(selector)
        if await loc.count() == 0:
            return None
        val = await loc.first.get_attribute(attr)
        return val.strip() if val else None

    async def _set_postal_code(self, page: Page, postal_code: str) -> None:
        trigger = page.locator(AMAZON_SELECTORS.glow_trigger)
        if await trigger.count() == 0:
            raise ExtractionError("No se encontró el botón de ubicación GLUX en Amazon.")
        await trigger.first.click()
        zip_input = page.locator(AMAZON_SELECTORS.glow_zip_input)
        await zip_input.fill(postal_code)
        await page.locator(AMAZON_SELECTORS.glow_confirm).first.click()

    async def _is_in_stock(self, page: Page) -> bool:
        has_cart = await page.locator(AMAZON_SELECTORS.add_to_cart).count() > 0
        avail_text = (await self._first_text(page, AMAZON_SELECTORS.availability) or "").lower()
        delivery_text = (await self._first_text(page, AMAZON_SELECTORS.delivery_block) or "").lower()
        if "no disponible" in avail_text or "no se puede enviar" in delivery_text:
            return False
        return has_cart or "disponible" in avail_text

    async def fetch(
        self,
        context: BrowserContext,
        url: str,
        postal_codes: Sequence[str],
    ) -> ProductSnapshot:
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            if resp is not None and resp.status in (403, 429, 503):
                raise BlockedError(f"Amazon respondió HTTP {resp.status}.")
            await self._check_blocked(page)

            title = await self._first_text(page, AMAZON_SELECTORS.title)
            price_raw = await self._first_text(page, AMAZON_SELECTORS.price)
            price = parse_price(price_raw)
            if not title or price is None:
                raise ExtractionError("No fue posible extraer título o precio en Amazon.")

            list_raw = await self._first_text(page, AMAZON_SELECTORS.list_price)
            list_price = parse_price(list_raw)

            seller_name = await self._first_text(page, AMAZON_SELECTORS.seller) or ""
            merchant_id = await self._first_attr(page, AMAZON_SELECTORS.merchant_id_input, "value")
            asin = await self._first_attr(page, AMAZON_SELECTORS.asin_input, "value")
            if not asin:
                match = _ASIN_URL_RE.search(url)
                asin = match.group(1).upper() if match else url

            availability: list[Availability] = []
            for cp in postal_codes:
                await self._set_postal_code(page, cp)
                await page.goto(url, wait_until="domcontentloaded")
                await self._check_blocked(page)
                in_stock = await self._is_in_stock(page)
                availability.append(Availability(postal_code=cp, in_stock=in_stock))

            official = is_official_seller(self.store, seller_name, merchant_id)
            return ProductSnapshot(
                store=self.store,
                sku=asin,
                title=title,
                url=url,
                price=price,
                list_price=list_price,
                seller=Seller(name=seller_name, seller_id=merchant_id, is_official=official),
                availability=availability,
            )
        finally:
            await page.close()

