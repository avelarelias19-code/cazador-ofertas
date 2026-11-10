"""Clases base, excepciones y utilidades de parseo para los scrapers de tiendas."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Sequence

from playwright.async_api import BrowserContext

from ofertas_monitor.models import ProductSnapshot, Store

_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


class BlockedError(RuntimeError):
    """La tienda presentó un CAPTCHA, reto anti-bot o bloqueo HTTP (403/429).

    Por diseño nunca se intenta resolver ni evadir el reto: la tienda entra en
    pausa (backoff) y se avisa al operador.
    """


class ExtractionError(RuntimeError):
    """No fue posible extraer los datos del producto (estructura cambiada o incompleta)."""


def parse_price(raw: object) -> Decimal | None:
    """Convierte un precio en texto o número a Decimal, o None si no es válido."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    if isinstance(raw, str):
        match = _PRICE_RE.search(raw)
        if not match:
            return None
        cleaned = match.group(1).replace(",", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


class StoreScraper(ABC):
    """Contrato común para todos los adaptadores de tienda."""

    store: ClassVar[Store]

    @abstractmethod
    async def fetch(
        self,
        context: BrowserContext,
        url: str,
        postal_codes: Sequence[str],
    ) -> ProductSnapshot:
        """Extrae snapshot del producto y verifica stock en cada código postal."""

