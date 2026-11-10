"""Scrapers Playwright para Office Depot México, La Comer, Suburbia y Coppel."""
from __future__ import annotations

import re
from typing import Sequence

from playwright.async_api import BrowserContext, Page

from ofertas_monitor.models import Availability, ProductSnapshot, Seller, Store
from ofertas_monitor.seller_filter import is_official_seller
from ofertas_monitor.stores.base import BlockedError, ExtractionError, StoreScraper, parse_price
from ofertas_monitor.stores.jsonld import extract_jsonld_product
from ofertas_monitor.stores.selectors import (
    COPPEL_SELECTORS,
    LA_COMER_SELECTORS,
    OFFICE_DEPOT_SELECTORS,
    SUBURBIA_SELECTORS,
    CoppelSelectors,
    LaComerSelectors,
    OfficeDepotSelectors,
    SuburbiaSelectors,
)

_URL_SKU_RE = re.compile(r"/p/([A-Za-z0-9_-]+)|/(\d{5,})(?:[/?#]|$)")


class _DirectRetailScraper(StoreScraper):
    """Base para tiendas departamentales/autoservicio con JSON-LD + DOM + modal de CP."""

    selectors: OfficeDepotSelectors | LaComerSelectors | SuburbiaSelectors | CoppelSelectors
    default_seller_name: str

    async def _check_blocked(self, page: Page) -> None:
        for marker in self.selectors.captcha_markers:
            if await page.locator(marker).count() > 0:
                raise BlockedError(f"{self.store.label} presentó reto anti-bot (respetado sin bypass).")

    async def _first_text(self, page: Page, selector: str) -> str | None:
        loc = page.locator(selector)
        if await loc.count() == 0:
            return None
        return (await loc.first.inner_text()).strip()

    async def _extract_sku(self, page: Page, url: str, jld_sku: str | None) -> str:
        if jld_sku:
            return jld_sku
        loc = page.locator(self.selectors.sku)
        if await loc.count() > 0:
            for attr in ("data-product-code", "data-sku", "content"):
                val = await loc.first.get_attribute(attr)
                if val:
                    return val.strip()
            txt = (await loc.first.inner_text()).strip()
            if txt:
                return txt
        match = _URL_SKU_RE.search(url)
        if match:
            return (match.group(1) or match.group(2) or url).strip()
        return url

    async def _set_postal_code(self, page: Page, postal_code: str) -> bool:
        trigger = page.locator(self.selectors.cp_trigger)
        if await trigger.count() == 0:
            return False
        await trigger.first.click()
        inp = page.locator(self.selectors.cp_input)
        await inp.first.fill(postal_code)
        await page.locator(self.selectors.cp_submit).first.click()
        return True

    async def _is_in_stock(self, page: Page, fallback_in_stock: bool) -> bool:
        msg = (await self._first_text(page, self.selectors.availability_msg) or "").lower()
        if "agotado" in msg or "sin cobertura" in msg or "no disponible" in msg:
            return False
        if await page.locator(self.selectors.add_to_cart).count() > 0:
            return True
        if "disponible" in msg or "en existencia" in msg:
            return True
        return fallback_in_stock

    async def fetch(
        self,
        context: BrowserContext,
        url: str,
        postal_codes: Sequence[str],
    ) -> ProductSnapshot:
        page: Page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            if resp is not None and resp.status in (403, 429):
                raise BlockedError(f"{self.store.label} respondió HTTP {resp.status}.")
            await self._check_blocked(page)

            jld = await extract_jsonld_product(page)
            title = (jld.title if jld else None) or await self._first_text(page, self.selectors.title)
            price = (jld.price if jld else None) or parse_price(
                await self._first_text(page, self.selectors.price)
            )
            list_price = (jld.list_price if jld else None) or parse_price(
                await self._first_text(page, self.selectors.list_price)
            )

            if not title or price is None:
                raise ExtractionError(
                    f"No se pudo extraer título o precio en {self.store.label} ({url!r})."
                )

            sku = await self._extract_sku(page, url, jld.sku if jld else None)
            dom_seller = await self._first_text(page, self.selectors.seller)
            seller_name = (
                (jld.seller_name if jld and jld.seller_name else None)
                or dom_seller
                or self.default_seller_name
            )
            base_in_stock = jld.in_stock if jld is not None else (
                await page.locator(self.selectors.add_to_cart).count() > 0
            )

            availability: list[Availability] = []
            for cp in postal_codes:
                changed = await self._set_postal_code(page, cp)
                if changed:
                    await page.reload(wait_until="domcontentloaded")
                    await self._check_blocked(page)
                in_stock = await self._is_in_stock(page, base_in_stock)
                availability.append(Availability(postal_code=cp, in_stock=in_stock))

            official = is_official_seller(self.store, seller_name, None)
            return ProductSnapshot(
                store=self.store,
                sku=sku,
                title=title,
                url=url,
                price=price,
                list_price=list_price,
                seller=Seller(name=seller_name, seller_id=None, is_official=official),
                availability=availability,
                image_url=jld.image_url if jld else None,
            )
        finally:
            await page.close()


class OfficeDepotScraper(_DirectRetailScraper):
    store = Store.OFFICE_DEPOT
    selectors = OFFICE_DEPOT_SELECTORS
    default_seller_name = "Office Depot"


class LaComerScraper(_DirectRetailScraper):
    store = Store.LA_COMER
    selectors = LA_COMER_SELECTORS
    default_seller_name = "La Comer"


class SuburbiaScraper(_DirectRetailScraper):
    store = Store.SUBURBIA
    selectors = SUBURBIA_SELECTORS
    default_seller_name = "Suburbia"


class CoppelScraper(_DirectRetailScraper):
    store = Store.COPPEL
    selectors = COPPEL_SELECTORS
    default_seller_name = "Coppel"


