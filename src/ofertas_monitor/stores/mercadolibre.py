"""Cliente híbrido para Mercado Libre México: API oficial + Playwright sin token."""
from __future__ import annotations

import json
import re
from typing import Any, Sequence

import httpx
from playwright.async_api import BrowserContext, Page

from ofertas_monitor.models import Availability, ProductSnapshot, Seller, Store
from ofertas_monitor.seller_filter import is_official_seller
from ofertas_monitor.stores.base import BlockedError, ExtractionError, StoreScraper, parse_price
from ofertas_monitor.stores.jsonld import extract_jsonld_product
from ofertas_monitor.stores.selectors import MERCADOLIBRE_SELECTORS

_ITEM_ID_QUERY_RE = re.compile(r"item_id:(MLM\d+)", re.IGNORECASE)
_ITEM_ID_PATH_RE = re.compile(r"\b(MLMU?)-?(\d{6,})\b", re.IGNORECASE)


def extract_item_id(url: str) -> str:
    """Extrae el identificador MLM de una URL de producto de Mercado Libre."""
    q_match = _ITEM_ID_QUERY_RE.search(url)
    if q_match:
        return q_match.group(1).upper()

    p_match = _ITEM_ID_PATH_RE.search(url)
    if p_match:
        return f"{p_match.group(1).upper()}{p_match.group(2)}"

    raise ExtractionError(f"No se pudo extraer el ID MLM de la URL: {url!r}")


class MercadoLibreScraper(StoreScraper):
    store = Store.MERCADOLIBRE

    def __init__(
        self,
        *,
        access_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.mercadolibre.com",
    ) -> None:
        self._access_token = (access_token or "").strip() or None
        self._client = http_client
        self._base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _resolve_seller_name(
        self,
        client: httpx.AsyncClient,
        official_store_id: int | str | None,
        seller_id: int | str | None,
    ) -> str:
        if official_store_id is not None:
            resp = await client.get(
                f"{self._base_url}/official_stores/{official_store_id}",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                name = resp.json().get("name")
                if name:
                    return str(name)
            return f"Tienda Oficial #{official_store_id}"

        if seller_id is not None:
            resp = await client.get(
                f"{self._base_url}/users/{seller_id}",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                nick = resp.json().get("nickname")
                if nick:
                    return str(nick)
            return str(seller_id)

        return "Desconocido"

    async def _check_cp_shipping(
        self,
        client: httpx.AsyncClient,
        item_id: str,
        postal_code: str,
    ) -> Availability:
        resp = await client.get(
            f"{self._base_url}/items/{item_id}/shipping_options",
            params={"zip_code": postal_code},
            headers=self._headers(),
        )
        if resp.status_code in (401, 403, 429):
            raise BlockedError(f"API Mercado Libre devolvió HTTP {resp.status_code} en shipping_options.")
        if resp.status_code != 200:
            return Availability(postal_code=postal_code, in_stock=False)
        data = resp.json()
        options = data.get("options") or []
        return Availability(postal_code=postal_code, in_stock=len(options) > 0)

    async def _check_blocked_page(self, page: Page) -> None:
        for selector in MERCADOLIBRE_SELECTORS.captcha_markers:
            if await page.locator(selector).count() > 0:
                raise BlockedError("Mercado Libre presentó CAPTCHA (respetado sin bypass).")

    async def _set_postal_code_playwright(self, page: Page, postal_code: str) -> bool:
        trigger = page.locator(MERCADOLIBRE_SELECTORS.cp_trigger)
        if await trigger.count() == 0:
            return False
        await trigger.first.click()
        inp = page.locator(MERCADOLIBRE_SELECTORS.cp_input)
        await inp.first.fill(postal_code)
        await page.locator(MERCADOLIBRE_SELECTORS.cp_submit).first.click()
        return True

    async def _is_in_stock_playwright(self, page: Page, fallback_in_stock: bool) -> bool:
        ship_loc = page.locator(MERCADOLIBRE_SELECTORS.shipping_summary)
        if await ship_loc.count() > 0:
            ship_text = (await ship_loc.first.inner_text()).lower()
            if "no llega" in ship_text or "sin envío" in ship_text or "no disponible" in ship_text:
                return False
            if "llega" in ship_text or "envío gratis" in ship_text:
                return await page.locator(MERCADOLIBRE_SELECTORS.buy_button).count() > 0
        if await page.locator(MERCADOLIBRE_SELECTORS.buy_button).count() > 0:
            return True
        return fallback_in_stock

    async def _fetch_via_playwright(
        self,
        context: BrowserContext,
        url: str,
        item_id: str,
        postal_codes: Sequence[str],
    ) -> ProductSnapshot:
        """Modo navegador sin token: extrae JSON-LD / __PRELOADED_STATE__ / DOM y prueba los 5 CPs."""
        page: Page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            if resp is not None and resp.status in (403, 429):
                raise BlockedError(f"Mercado Libre web respondió HTTP {resp.status}.")
            await self._check_blocked_page(page)

            jld = await extract_jsonld_product(page)
            title = jld.title if jld else ""
            price = jld.price if jld else None
            list_price = jld.list_price if jld else None

            if not title:
                loc = page.locator(MERCADOLIBRE_SELECTORS.title)
                if await loc.count() > 0:
                    title = (await loc.first.inner_text()).strip()

            if price is None:
                loc = page.locator(MERCADOLIBRE_SELECTORS.price)
                if await loc.count() > 0:
                    price = parse_price(await loc.first.inner_text())

            if list_price is None:
                loc = page.locator(MERCADOLIBRE_SELECTORS.list_price)
                if await loc.count() > 0:
                    list_price = parse_price(await loc.first.inner_text())

            if not title or price is None:
                raise ExtractionError(f"No se pudo extraer título o precio en Mercado Libre ({url!r}).")

            official_store_id: int | str | None = None
            seller_id: str | None = None
            seller_name = (jld.seller_name if jld else None) or ""

            state_loc = page.locator(MERCADOLIBRE_SELECTORS.preloaded_state)
            if await state_loc.count() > 0:
                try:
                    raw_state = json.loads(await state_loc.first.inner_text())
                    initial = raw_state.get("initialState", raw_state)
                    official_store_id = initial.get("official_store_id")
                    if initial.get("seller_id") is not None:
                        seller_id = str(initial["seller_id"])
                    if not seller_name and initial.get("seller_name"):
                        seller_name = str(initial["seller_name"])
                except json.JSONDecodeError:
                    pass

            if not seller_name:
                s_loc = page.locator(MERCADOLIBRE_SELECTORS.seller_name)
                if await s_loc.count() > 0:
                    seller_name = (await s_loc.first.inner_text()).strip()

            if official_store_id is None:
                badge = page.locator(MERCADOLIBRE_SELECTORS.official_badge)
                if await badge.count() > 0:
                    badge_text = (await badge.first.inner_text()).lower()
                    if "tienda oficial" in badge_text:
                        official_store_id = "dom_official_badge"

            base_in_stock = jld.in_stock if jld is not None else False
            availability: list[Availability] = []
            for cp in postal_codes:
                changed = await self._set_postal_code_playwright(page, cp)
                if changed:
                    await page.goto(url, wait_until="domcontentloaded")
                    await self._check_blocked_page(page)
                in_stock = await self._is_in_stock_playwright(page, base_in_stock)
                availability.append(Availability(postal_code=cp, in_stock=in_stock))

            official = is_official_seller(
                self.store,
                seller_name or "Mercado Libre",
                seller_id,
                official_store_id=official_store_id,
            )
            return ProductSnapshot(
                store=self.store,
                sku=item_id,
                title=title,
                url=url,
                price=price,
                list_price=list_price,
                seller=Seller(
                    name=seller_name or "Mercado Libre",
                    seller_id=seller_id,
                    is_official=official,
                ),
                availability=availability,
                image_url=jld.image_url if jld else None,
            )
        finally:
            await page.close()

    async def fetch(
        self,
        context: BrowserContext | None,
        url: str,
        postal_codes: Sequence[str],
    ) -> ProductSnapshot:
        item_id = extract_item_id(url)

        # Si no hay token configurado ni cliente HTTP inyectado y tenemos Playwright,
        # intentamos primero la API y ante cualquier 401/403 o fallo de red caemos a Playwright.
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            try:
                resp = await client.get(f"{self._base_url}/items/{item_id}", headers=self._headers())
            except httpx.HTTPError:
                if context is not None and owns_client:
                    return await self._fetch_via_playwright(context, url, item_id, postal_codes)
                raise

            if resp.status_code in (401, 403) and context is not None and owns_client:
                return await self._fetch_via_playwright(context, url, item_id, postal_codes)
            if resp.status_code in (401, 403, 429):
                raise BlockedError(f"API Mercado Libre devolvió HTTP {resp.status_code} para {item_id}.")
            if resp.status_code != 200:
                raise ExtractionError(f"No se pudo consultar el ítem {item_id} (HTTP {resp.status_code}).")

            item: dict[str, Any] = resp.json()
            price = parse_price(item.get("price"))
            if price is None:
                raise ExtractionError(f"Ítem {item_id} sin precio en Mercado Libre.")
            list_price = parse_price(item.get("original_price"))

            official_store_id = item.get("official_store_id")
            seller_id = item.get("seller_id")
            seller_name = await self._resolve_seller_name(client, official_store_id, seller_id)
            official = is_official_seller(
                self.store,
                seller_name,
                str(seller_id) if seller_id is not None else None,
                official_store_id=official_store_id,
            )

            active_and_available = (
                str(item.get("status", "")).lower() == "active"
                and int(item.get("available_quantity") or 0) > 0
            )

            availability: list[Availability] = []
            if not active_and_available:
                availability = [Availability(postal_code=cp, in_stock=False) for cp in postal_codes]
            else:
                for cp in postal_codes:
                    availability.append(await self._check_cp_shipping(client, item_id, cp))

            pictures = item.get("pictures") or []
            image_url = (
                pictures[0].get("secure_url") or pictures[0].get("url")
                if pictures and isinstance(pictures[0], dict)
                else None
            )

            return ProductSnapshot(
                store=self.store,
                sku=str(item.get("id") or item_id),
                title=str(item.get("title") or "").strip(),
                url=str(item.get("permalink") or url),
                price=price,
                list_price=list_price,
                seller=Seller(
                    name=seller_name,
                    seller_id=str(seller_id) if seller_id is not None else None,
                    is_official=official,
                ),
                availability=availability,
                currency=str(item.get("currency_id") or "MXN"),
                image_url=image_url,
            )
        finally:
            if owns_client:
                await client.aclose()
