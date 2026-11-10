"""Descubridor automático de ofertas por búsqueda/categoría en las 5 tiendas.

Pre-filtra productos que ya muestran un descuento candidato (>65%, o >=30% en el
rango de >=$10,000 MXN, o precio simbólico) y devuelve la URL directa de cada
ficha de producto para que el Monitor verifique vendedor oficial y stock en los 5 CPs.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Page

from ofertas_monitor.config import SearchQuery, Target
from ofertas_monitor.detection import DealRules, is_candidate_discount
from ofertas_monitor.models import Store
from ofertas_monitor.seller_filter import is_official_seller
from ofertas_monitor.stores.base import parse_price

logger = logging.getLogger(__name__)

_VTEX_ORIGINS: dict[Store, str] = {
    Store.CHEDRAUI: "https://www.chedraui.com.mx",
    Store.ELEKTRA: "https://www.elektra.mx",
    Store.BODEGAS_ALIANZA: "https://www.bodegasalianza.com",
}


class StoreDiscoverer:
    def __init__(self, rules: DealRules = DealRules()) -> None:
        self.rules = rules

    async def discover(
        self,
        context: BrowserContext,
        queries: Sequence[SearchQuery],
    ) -> list[Target]:
        discovered: list[Target] = []
        seen_urls: set[str] = set()

        for sq in queries:
            try:
                if sq.store in _VTEX_ORIGINS:
                    urls = await self._discover_vtex(context, sq)
                elif sq.store is Store.WALMART:
                    urls = await self._discover_walmart(context, sq)
                elif sq.store is Store.AMAZON:
                    urls = await self._discover_amazon(context, sq)
                elif sq.store is Store.MERCADOLIBRE:
                    urls = await self._discover_mercadolibre(context, sq)
                elif sq.store is Store.OFFICE_DEPOT:
                    urls = await self._discover_office_depot(context, sq)
                elif sq.store is Store.LA_COMER:
                    urls = await self._discover_la_comer(context, sq)
                elif sq.store is Store.SUBURBIA:
                    urls = await self._discover_suburbia(context, sq)
                elif sq.store is Store.COPPEL:
                    urls = await self._discover_coppel(context, sq)
                else:
                    urls = []
            except Exception as exc:
                logger.warning("Error descubriendo '%s' en %s: %s", sq.query, sq.store.label, exc)
                continue

            for url in urls:
                if url not in seen_urls:
                    seen_urls.add(url)
                    discovered.append(Target(store=sq.store, url=url))

        return discovered

    async def _discover_vtex(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        origin = _VTEX_ORIGINS[sq.store]
        to_idx = max(0, sq.max_results - 1)
        api_url = f"{origin}/api/catalog_system/pub/products/search/{quote_plus(sq.query)}?_from=0&_to={to_idx}"

        page: Page = await context.new_page()
        try:
            await page.goto(origin, wait_until="domcontentloaded")
            result = await page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {headers: {'Accept': 'application/json'}});
                    if (r.status !== 200) return [];
                    try { return await r.json(); } catch (_) { return []; }
                }""",
                api_url,
            )
            if not isinstance(result, list):
                return []

            urls: list[str] = []
            for prod in result:
                if not isinstance(prod, dict):
                    continue
                link_text = str(prod.get("linkText") or "").strip()
                items = prod.get("items") or []
                if not link_text or not items:
                    continue
                sellers = items[0].get("sellers") or []
                # Buscar seller oficial ("1") con oferta candidata
                for s in sellers:
                    sid = str(s.get("sellerId") or "").strip()
                    sname = str(s.get("sellerName") or "").strip()
                    if not is_official_seller(sq.store, sname, sid):
                        continue
                    offer = s.get("commertialOffer") or {}
                    price = parse_price(offer.get("Price"))
                    list_price = parse_price(offer.get("ListPrice") or offer.get("PriceWithoutDiscount"))
                    if price is not None and is_candidate_discount(price, list_price, self.rules):
                        urls.append(f"{origin}/{link_text}/p")
                        break
            return urls
        finally:
            await page.close()

    async def _discover_walmart(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        origin = "https://www.walmart.com.mx"
        search_url = f"{origin}/search?q={quote_plus(sq.query)}"
        page: Page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            loc = page.locator("script#__NEXT_DATA__")
            if await loc.count() == 0:
                return []
            try:
                data: dict[str, Any] = json.loads(await loc.first.inner_text())
            except json.JSONDecodeError:
                return []

            stacks = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialData", {})
                .get("searchResult", {})
                .get("itemStacks")
                or []
            )
            urls: list[str] = []
            for stack in stacks:
                for item in (stack.get("items") or [])[: sq.max_results]:
                    if not isinstance(item, dict):
                        continue
                    seller_name = str(item.get("sellerName") or "Walmart")
                    seller_id = str(item["sellerId"]) if item.get("sellerId") is not None else None
                    if not is_official_seller(Store.WALMART, seller_name, seller_id):
                        continue

                    pinfo = item.get("priceInfo") or {}
                    curr = pinfo.get("currentPrice") or {}
                    was = pinfo.get("wasPrice") or {}
                    price = parse_price(curr.get("price") if isinstance(curr, dict) else None)
                    list_price = parse_price(was.get("price") if isinstance(was, dict) else None)
                    if price is None or not is_candidate_discount(price, list_price, self.rules):
                        continue

                    canonical = str(item.get("canonicalUrl") or "").strip()
                    us_item_id = str(item.get("usItemId") or "").strip()
                    if canonical.startswith("/ip/"):
                        urls.append(f"{origin}{canonical}")
                    elif us_item_id:
                        urls.append(f"{origin}/ip/producto/{us_item_id}")
            return urls
        finally:
            await page.close()

    async def _discover_amazon(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        origin = "https://www.amazon.com.mx"
        search_url = f"{origin}/s?k={quote_plus(sq.query)}&rh=p_6%3AAVDBXBAVVSXLQ"
        page: Page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            cards = page.locator("[data-component-type='s-search-result'][data-asin]")
            count = min(await cards.count(), sq.max_results)
            urls: list[str] = []
            for idx in range(count):
                card = cards.nth(idx)
                asin = (await card.get_attribute("data-asin") or "").strip()
                if not asin:
                    continue
                price_loc = card.locator(".a-price:not([data-a-strike='true']) .a-offscreen")
                list_loc = card.locator(".a-price[data-a-strike='true'] .a-offscreen")
                if await price_loc.count() == 0 or await list_loc.count() == 0:
                    continue
                price = parse_price(await price_loc.first.inner_text())
                list_price = parse_price(await list_loc.first.inner_text())
                if price is not None and is_candidate_discount(price, list_price, self.rules):
                    urls.append(f"{origin}/dp/{asin}")
            return urls
        finally:
            await page.close()

    async def _discover_mercadolibre(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        slug = sq.query.strip().replace(" ", "-")
        # _Tienda_all filtra únicamente Tiendas Oficiales en el listado de Mercado Libre
        search_url = f"https://listado.mercadolibre.com.mx/{quote_plus(slug)}_Tienda_all"
        page: Page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            cards = page.locator("li.ui-search-layout__item")
            count = min(await cards.count(), sq.max_results)
            urls: list[str] = []
            for idx in range(count):
                card = cards.nth(idx)
                link_loc = card.locator("a.poly-component__title, a.ui-search-link")
                price_loc = card.locator(
                    ".poly-price__current .andes-money-amount__fraction, "
                    ".ui-search-price__second-line .andes-money-amount__fraction"
                )
                list_loc = card.locator(
                    "s.andes-money-amount .andes-money-amount__fraction, "
                    ".ui-search-price__original-value .andes-money-amount__fraction"
                )
                if await link_loc.count() == 0 or await price_loc.count() == 0 or await list_loc.count() == 0:
                    continue
                href = (await link_loc.first.get_attribute("href") or "").strip()
                price = parse_price(await price_loc.first.inner_text())
                list_price = parse_price(await list_loc.first.inner_text())
                if href.startswith("https://") and price is not None and is_candidate_discount(price, list_price, self.rules):
                    urls.append(href.split("#")[0])
            return urls
        finally:
            await page.close()

    async def _discover_office_depot(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        origin = "https://www.officedepot.com.mx"
        search_url = f"{origin}/officedepot/es/search/?text={quote_plus(sq.query)}"
        page: Page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            cards = page.locator(".product-item, .product__list--item, [data-product-code]")
            count = min(await cards.count(), sq.max_results)
            urls: list[str] = []
            for idx in range(count):
                card = cards.nth(idx)
                link_loc = card.locator("a[href*='/p/']")
                price_loc = card.locator(".pdp-price-current, .discountedPrice, .price-current")
                list_loc = card.locator(".pdp-price-before, .beforePrice, s.price-before")
                if await link_loc.count() == 0 or await price_loc.count() == 0 or await list_loc.count() == 0:
                    continue
                href = (await link_loc.first.get_attribute("href") or "").strip()
                price = parse_price(await price_loc.first.inner_text())
                list_price = parse_price(await list_loc.first.inner_text())
                if price is not None and is_candidate_discount(price, list_price, self.rules):
                    full_url = href if href.startswith("https://") else f"{origin}{href}"
                    urls.append(full_url)
            return urls
        finally:
            await page.close()

    async def _discover_la_comer(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        origin = "https://www.lacomer.com.mx"
        search_url = f"{origin}/lacomer/#!/item-search/0/{quote_plus(sq.query)}/true?p=1"
        page: Page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            cards = page.locator(".product-card, .item-product, [data-product-url]")
            count = min(await cards.count(), sq.max_results)
            urls: list[str] = []
            for idx in range(count):
                card = cards.nth(idx)
                link_loc = card.locator("a[href]")
                price_loc = card.locator(".price-promo, .precio-oferta, .product-price")
                list_loc = card.locator(".price-regular, .precio-normal, s.price-before")
                if await link_loc.count() == 0 or await price_loc.count() == 0 or await list_loc.count() == 0:
                    continue
                href = (await link_loc.first.get_attribute("href") or "").strip()
                price = parse_price(await price_loc.first.inner_text())
                list_price = parse_price(await list_loc.first.inner_text())
                if price is not None and is_candidate_discount(price, list_price, self.rules):
                    full_url = href if href.startswith("https://") else f"{origin}{href}"
                    urls.append(full_url)
            return urls
        finally:
            await page.close()

    async def _discover_suburbia(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        origin = "https://www.suburbia.com.mx"
        search_url = f"{origin}/tienda?s={quote_plus(sq.query)}"
        page: Page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            cards = page.locator(".m-product__card, .product-card, .product-item, [data-prodid]")
            count = min(await cards.count(), sq.max_results)
            urls: list[str] = []
            for idx in range(count):
                card = cards.nth(idx)
                link_loc = card.locator("a[href*='/tienda/pdp/'], a[href*='/p/'], a[href]")
                price_loc = card.locator(
                    ".a-card-discount, .pdp-price-current, .price-promo, .discountedPrice"
                )
                list_loc = card.locator(
                    ".a-card-price, .pdp-price-before, .price-regular, s.price-before"
                )
                if await link_loc.count() == 0 or await price_loc.count() == 0 or await list_loc.count() == 0:
                    continue
                href = (await link_loc.first.get_attribute("href") or "").strip()
                price = parse_price(await price_loc.first.inner_text())
                list_price = parse_price(await list_loc.first.inner_text())
                if price is not None and is_candidate_discount(price, list_price, self.rules):
                    full_url = href if href.startswith("https://") else f"{origin}{href}"
                    urls.append(full_url)
            return urls
        finally:
            await page.close()

    async def _discover_coppel(self, context: BrowserContext, sq: SearchQuery) -> list[str]:
        origin = "https://www.coppel.com"
        search_url = f"{origin}/SearchDisplay?searchTerm={quote_plus(sq.query)}"
        page: Page = await context.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            cards = page.locator(".product-card, .product-item, [data-testid='product-card'], [data-sku]")
            count = min(await cards.count(), sq.max_results)
            urls: list[str] = []
            for idx in range(count):
                card = cards.nth(idx)
                link_loc = card.locator("a[href*='/p/'], a[href*='-pm-'], a[href]")
                price_loc = card.locator(
                    ".pdp-price-current, .price-discounted, .price-promo, .discountedPrice"
                )
                list_loc = card.locator(
                    ".pdp-price-old, .pdp-price-before, .price-regular, s.price-before"
                )
                if await link_loc.count() == 0 or await price_loc.count() == 0 or await list_loc.count() == 0:
                    continue
                href = (await link_loc.first.get_attribute("href") or "").strip()
                price = parse_price(await price_loc.first.inner_text())
                list_price = parse_price(await list_loc.first.inner_text())
                if price is not None and is_candidate_discount(price, list_price, self.rules):
                    full_url = href if href.startswith("https://") else f"{origin}{href}"
                    urls.append(full_url)
            return urls
        finally:
            await page.close()


