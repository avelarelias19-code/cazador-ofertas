"""Extractor genérico de datos estructurados schema.org/Product (JSON-LD)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from playwright.async_api import Page

from ofertas_monitor.stores.base import parse_price


@dataclass(frozen=True)
class JsonLdProduct:
    title: str
    sku: str
    price: Decimal
    list_price: Decimal | None
    seller_name: str | None
    in_stock: bool
    image_url: str | None = None


def _is_product_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    raw_type = node.get("@type")
    if isinstance(raw_type, str):
        return raw_type.lower().endswith("product")
    if isinstance(raw_type, list):
        return any(isinstance(t, str) and t.lower().endswith("product") for t in raw_type)
    return False


def _find_product_node(data: Any) -> dict[str, Any] | None:
    if _is_product_node(data):
        return data
    if isinstance(data, list):
        for item in data:
            found = _find_product_node(item)
            if found is not None:
                return found
    elif isinstance(data, dict):
        for key in ("@graph", "mainEntity"):
            if key in data:
                found = _find_product_node(data[key])
                if found is not None:
                    return found
    return None


def _extract_image(raw_image: Any) -> str | None:
    if isinstance(raw_image, str):
        return raw_image
    if isinstance(raw_image, list) and raw_image:
        return _extract_image(raw_image[0])
    if isinstance(raw_image, dict):
        url = raw_image.get("url") or raw_image.get("contentUrl")
        return str(url) if url else None
    return None


def _extract_list_price(offer: dict[str, Any]) -> Decimal | None:
    specs = offer.get("priceSpecification")
    if isinstance(specs, dict):
        specs = [specs]
    if isinstance(specs, list):
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            price_type = str(spec.get("priceType", "")).lower()
            if "listprice" in price_type or "strikethrough" in price_type:
                parsed = parse_price(spec.get("price"))
                if parsed is not None:
                    return parsed
    return parse_price(offer.get("highPrice"))


async def extract_jsonld_product(page: Page) -> JsonLdProduct | None:
    """Busca y parsea el primer bloque JSON-LD de tipo schema.org/Product en la página."""
    scripts = await page.locator("script[type='application/ld+json']").all_inner_texts()
    for raw_text in scripts:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            continue

        product = _find_product_node(payload)
        if product is None:
            continue

        offers = product.get("offers")
        if isinstance(offers, list):
            offer = offers[0] if offers else {}
        elif isinstance(offers, dict):
            offer = offers
        else:
            offer = {}

        price = parse_price(offer.get("price") or offer.get("lowPrice"))
        if price is None:
            continue

        list_price = _extract_list_price(offer)
        seller = offer.get("seller")
        if isinstance(seller, dict):
            seller_name = seller.get("name")
        elif isinstance(seller, str):
            seller_name = seller
        else:
            seller_name = None

        avail_raw = str(offer.get("availability", "")).lower()
        in_stock = "instock" in avail_raw

        return JsonLdProduct(
            title=str(product.get("name") or "").strip(),
            sku=str(product.get("sku") or product.get("productID") or "").strip(),
            price=price,
            list_price=list_price,
            seller_name=str(seller_name).strip() if seller_name else None,
            in_stock=in_stock,
            image_url=_extract_image(product.get("image")),
        )

    return None

