"""Scraper Playwright para Walmart México (__NEXT_DATA__ + fallback JSON-LD)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from playwright.async_api import BrowserContext, Page

from ofertas_monitor.models import Availability, ProductSnapshot, Seller, Store
from ofertas_monitor.seller_filter import is_official_seller
from ofertas_monitor.stores.base import BlockedError, ExtractionError, StoreScraper, parse_price
from ofertas_monitor.stores.jsonld import extract_jsonld_product
from ofertas_monitor.stores.selectors import WALMART_SELECTORS


@dataclass
class _WalmartParsed:
    sku: str
    title: str
    price: Decimal
    list_price: Decimal | None
    seller_name: str
    seller_id: str | None
    in_stock: bool
    image_url: str | None
    canonical_url: str | None = None


class WalmartScraper(StoreScraper):
    store = Store.WALMART

    async def _check_blocked(self, page: Page) -> None:
        for selector in WALMART_SELECTORS.captcha_markers:
            if await page.locator(selector).count() > 0:
                raise BlockedError("Walmart presentó reto anti-bot (px-captcha).")
        title = (await page.title()).lower()
        if "robot or human" in title or "verifica tu identidad" in title:
            raise BlockedError("Walmart solicitó verificación de identidad.")

    async def _extract_from_next_data(self, page: Page) -> _WalmartParsed | None:
        loc = page.locator(WALMART_SELECTORS.next_data)
        if await loc.count() == 0:
            return None
        raw = await loc.first.inner_text()
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            return None

        product = (
            data.get("props", {})
            .get("pageProps", {})
            .get("initialData", {})
            .get("data", {})
            .get("product")
        )
        if not isinstance(product, dict):
            return None

        price_info = product.get("priceInfo") or {}
        curr = price_info.get("currentPrice") or {}
        was = price_info.get("wasPrice") or {}
        price = parse_price(curr.get("price") if isinstance(curr, dict) else None)
        if price is None:
            return None
        list_price = parse_price(was.get("price") if isinstance(was, dict) else None)

        avail_status = str(product.get("availabilityStatus") or "").upper()
        in_stock = avail_status == "IN_STOCK"

        image_info = product.get("imageInfo") or {}
        canonical = str(product.get("canonicalUrl") or "").strip()
        canonical_full = f"https://www.walmart.com.mx{canonical}" if canonical.startswith("/ip/") else None
        return _WalmartParsed(
            sku=str(product.get("usItemId") or product.get("id") or "").strip(),
            title=str(product.get("name") or "").strip(),
            price=price,
            list_price=list_price,
            seller_name=str(product.get("sellerName") or "").strip(),
            seller_id=str(product["sellerId"]) if product.get("sellerId") is not None else None,
            in_stock=in_stock,
            image_url=image_info.get("thumbnailUrl") if isinstance(image_info, dict) else None,
            canonical_url=canonical_full,
        )

    async def _extract_page(self, page: Page) -> _WalmartParsed:
        await self._check_blocked(page)
        parsed = await self._extract_from_next_data(page)
        if parsed is not None:
            return parsed

        jld = await extract_jsonld_product(page)
        if jld is not None:
            return _WalmartParsed(
                sku=jld.sku,
                title=jld.title,
                price=jld.price,
                list_price=jld.list_price,
                seller_name=jld.seller_name or "",
                seller_id=None,
                in_stock=jld.in_stock,
                image_url=jld.image_url,
            )

        raise ExtractionError("No se encontró __NEXT_DATA__ ni JSON-LD válido en Walmart.")

    async def _set_postal_code(self, page: Page, postal_code: str) -> None:
        trigger = page.locator(WALMART_SELECTORS.fulfillment_trigger)
        if await trigger.count() == 0:
            raise ExtractionError("No se encontró el selector de código postal en Walmart.")
        await trigger.first.click()
        inp = page.locator(WALMART_SELECTORS.postal_code_input)
        await inp.fill(postal_code)
        await page.locator(WALMART_SELECTORS.save_postal_code).click()

    async def fetch(
        self,
        context: BrowserContext,
        url: str,
        postal_codes: Sequence[str],
    ) -> ProductSnapshot:
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            if resp is not None and resp.status in (403, 429):
                raise BlockedError(f"Walmart respondió HTTP {resp.status}.")

            base = await self._extract_page(page)
            availability: list[Availability] = []

            for cp in postal_codes:
                await self._set_postal_code(page, cp)
                await page.goto(url, wait_until="domcontentloaded")
                cp_data = await self._extract_page(page)
                availability.append(Availability(postal_code=cp, in_stock=cp_data.in_stock))

            official = is_official_seller(self.store, base.seller_name, base.seller_id)
            direct_url = base.canonical_url or url
            return ProductSnapshot(
                store=self.store,
                sku=base.sku,
                title=base.title,
                url=direct_url,
                price=base.price,
                list_price=base.list_price,
                seller=Seller(
                    name=base.seller_name,
                    seller_id=base.seller_id,
                    is_official=official,
                ),
                availability=availability,
                image_url=base.image_url,
            )
        finally:
            await page.close()

