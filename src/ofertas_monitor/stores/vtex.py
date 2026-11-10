"""Scrapers para tiendas basadas en VTEX (Chedraui y Elektra).

Usa los endpoints públicos de catálogo y de simulación de carrito/envío de VTEX
desde el contexto del navegador para obtener precio, vendedor (sellerId '1' =
oficial) y disponibilidad real por código postal.
"""
from __future__ import annotations

import re
from typing import Any, Sequence
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page

from ofertas_monitor.models import Availability, ProductSnapshot, Seller, Store
from ofertas_monitor.seller_filter import is_official_seller
from ofertas_monitor.stores.base import BlockedError, ExtractionError, StoreScraper, parse_price

_SLUG_RE = re.compile(r"/([^/?#]+)/p(?:[/?#]|$)")


def _extract_slug_and_origin(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    match = _SLUG_RE.search(parsed.path)
    if not match:
        raise ExtractionError(f"La URL no tiene el formato VTEX '/<slug>/p': {url!r}")
    return origin, match.group(1)


class VtexScraper(StoreScraper):
    """Implementación compartida para tiendas VTEX (Chedraui y Elektra)."""

    async def _fetch_catalog(self, page: Page, origin: str, slug: str) -> list[dict[str, Any]]:
        catalog_url = f"{origin}/api/catalog_system/pub/products/search/{slug}/p"
        result = await page.evaluate(
            """async (url) => {
                const resp = await fetch(url, {headers: {'Accept': 'application/json'}});
                let body = null;
                try { body = await resp.json(); } catch (_) {}
                return {status: resp.status, body};
            }""",
            catalog_url,
        )
        status = int(result["status"])
        if status in (403, 429):
            raise BlockedError(f"{self.store.label} devolvió HTTP {status} en catálogo VTEX.")
        body = result["body"]
        if status != 200 or not isinstance(body, list) or not body:
            raise ExtractionError(f"Catálogo VTEX vacío o inválido para {slug!r} (HTTP {status}).")
        return body

    async def _simulate_cp(
        self,
        page: Page,
        origin: str,
        item_id: str,
        seller_id: str,
        postal_code: str,
    ) -> Availability:
        sim_url = f"{origin}/api/checkout/pub/orderForms/simulation"
        payload = {
            "items": [{"id": item_id, "quantity": 1, "seller": seller_id}],
            "postalCode": postal_code,
            "country": "MEX",
        }
        result = await page.evaluate(
            """async ({url, payload}) => {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                    body: JSON.stringify(payload),
                });
                let body = null;
                try { body = await resp.json(); } catch (_) {}
                return {status: resp.status, body};
            }""",
            {"url": sim_url, "payload": payload},
        )
        status = int(result["status"])
        if status in (403, 429):
            raise BlockedError(f"{self.store.label} devolvió HTTP {status} en simulación VTEX.")
        body = result["body"] or {}
        items = body.get("items") or []
        logistics = body.get("logisticsInfo") or []

        item_avail = bool(items and items[0].get("availability") == "available")
        slas = logistics[0].get("slas") if logistics and isinstance(logistics[0], dict) else []
        slas = slas if isinstance(slas, list) else []

        in_stock = item_avail and len(slas) > 0
        pickup = any(sla.get("deliveryChannel") == "pickup-in-point" for sla in slas if isinstance(sla, dict))
        return Availability(postal_code=postal_code, in_stock=in_stock, pickup=pickup)

    @staticmethod
    def _choose_seller(sellers: list[dict[str, Any]]) -> dict[str, Any]:
        for s in sellers:
            if str(s.get("sellerId")) == "1":
                return s
        for s in sellers:
            if s.get("sellerDefault"):
                return s
        return sellers[0]

    async def fetch(
        self,
        context: BrowserContext,
        url: str,
        postal_codes: Sequence[str],
    ) -> ProductSnapshot:
        origin, slug = _extract_slug_and_origin(url)
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
            if resp is not None and resp.status in (403, 429):
                raise BlockedError(f"{self.store.label} respondió HTTP {resp.status}.")

            catalog = await self._fetch_catalog(page, origin, slug)
            product = catalog[0]
            items = product.get("items") or []
            if not items:
                raise ExtractionError(f"Producto VTEX sin SKUs en {url!r}.")

            sku_item = items[0]
            item_id = str(sku_item.get("itemId") or product.get("productId") or "").strip()
            sellers = sku_item.get("sellers") or []
            if not sellers:
                raise ExtractionError(f"SKU {item_id} sin vendedores en {url!r}.")

            chosen = self._choose_seller(sellers)
            seller_id = str(chosen.get("sellerId") or "").strip()
            seller_name = str(chosen.get("sellerName") or self.store.label).strip()
            offer = chosen.get("commertialOffer") or {}

            price = parse_price(offer.get("Price"))
            if price is None:
                raise ExtractionError(f"SKU {item_id} sin precio en oferta comercial VTEX.")
            list_price = parse_price(offer.get("ListPrice") or offer.get("PriceWithoutDiscount"))

            images = sku_item.get("images") or []
            image_url = images[0].get("imageUrl") if images and isinstance(images[0], dict) else None

            availability: list[Availability] = []
            for cp in postal_codes:
                availability.append(
                    await self._simulate_cp(page, origin, item_id, seller_id, cp)
                )

            official = is_official_seller(self.store, seller_name, seller_id)
            return ProductSnapshot(
                store=self.store,
                sku=item_id,
                title=str(product.get("productName") or sku_item.get("name") or "").strip(),
                url=url,
                price=price,
                list_price=list_price,
                seller=Seller(name=seller_name, seller_id=seller_id, is_official=official),
                availability=availability,
                image_url=image_url,
            )
        finally:
            await page.close()


class ChedrauiScraper(VtexScraper):
    store = Store.CHEDRAUI


class ElektraScraper(VtexScraper):
    store = Store.ELEKTRA


class BodegasAlianzaScraper(VtexScraper):
    store = Store.BODEGAS_ALIANZA

