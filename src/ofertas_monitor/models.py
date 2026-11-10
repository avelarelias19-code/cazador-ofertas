"""Modelos de dominio para snapshots de productos, vendedores y disponibilidad."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class Store(str, Enum):
    WALMART = "walmart"
    CHEDRAUI = "chedraui"
    ELEKTRA = "elektra"
    AMAZON = "amazon"
    MERCADOLIBRE = "mercadolibre"
    OFFICE_DEPOT = "office_depot"
    LA_COMER = "la_comer"
    BODEGAS_ALIANZA = "bodegas_alianza"
    SUBURBIA = "suburbia"
    COPPEL = "coppel"

    @property
    def label(self) -> str:
        labels = {
            Store.WALMART: "Walmart",
            Store.CHEDRAUI: "Chedraui",
            Store.ELEKTRA: "Elektra",
            Store.AMAZON: "Amazon México",
            Store.MERCADOLIBRE: "Mercado Libre",
            Store.OFFICE_DEPOT: "Office Depot",
            Store.LA_COMER: "La Comer",
            Store.BODEGAS_ALIANZA: "Bodegas Alianza",
            Store.SUBURBIA: "Suburbia",
            Store.COPPEL: "Coppel",
        }
        return labels[self]


@dataclass(frozen=True)
class Seller:
    name: str
    seller_id: str | None = None
    is_official: bool = False


@dataclass(frozen=True)
class Availability:
    postal_code: str
    in_stock: bool
    pickup: bool = False
    detail: str | None = None


@dataclass
class ProductSnapshot:
    store: Store
    sku: str
    title: str
    url: str
    price: Decimal
    list_price: Decimal | None
    seller: Seller
    availability: list[Availability] = field(default_factory=list)
    currency: str = "MXN"
    image_url: str | None = None
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def discount_pct(self) -> float | None:
        if self.list_price is None or self.list_price <= 0:
            return None
        if self.price >= self.list_price:
            return 0.0
        pct = (self.list_price - self.price) / self.list_price * Decimal("100")
        return round(float(pct), 2)

    @property
    def in_stock_postal_codes(self) -> list[str]:
        return [a.postal_code for a in self.availability if a.in_stock]

