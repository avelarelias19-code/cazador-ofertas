"""Motor de evaluación de ofertas: descuento mínimo general, gama alta (>= $10,000) y errores de precio.

Los umbrales reales se leen de `config.yaml` (sección `rules`); los valores por defecto de
`DealRules` solo aplican si no hay configuración.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from ofertas_monitor.models import ProductSnapshot


class DealType(str, Enum):
    DISCOUNT = "discount"
    PRICE_ERROR = "price_error"


@dataclass(frozen=True)
class DealRules:
    min_discount_pct: float = 65.0
    high_ticket_min_price: Decimal = Decimal("10000")
    high_ticket_min_discount_pct: float = 30.0
    price_error_ratio: float = 0.35
    min_history_samples: int = 3
    symbolic_price_max: Decimal = Decimal("10")
    symbolic_list_min: Decimal = Decimal("500")


@dataclass
class Deal:
    snapshot: ProductSnapshot
    types: set[DealType] | frozenset[DealType]
    reasons: list[str] = field(default_factory=list)
    history_median: Decimal | None = None

    @property
    def discount_pct(self) -> float | None:
        return self.snapshot.discount_pct

    @property
    def in_stock_postal_codes(self) -> list[str]:
        return self.snapshot.in_stock_postal_codes

    @property
    def is_price_error(self) -> bool:
        return DealType.PRICE_ERROR in self.types


def is_candidate_discount(
    price: Decimal,
    list_price: Decimal | None,
    rules: DealRules | None = None,
) -> bool:
    """Verifica rápido si un par (precio, precio_lista) cumple algún umbral de descuento o precio simbólico."""
    active = rules or DealRules()
    if price <= 0 or list_price is None or list_price <= price:
        return False

    pct = round(float((list_price - price) / list_price * Decimal("100")), 2)
    if pct > active.min_discount_pct:
        return True

    ref_price = max(price, list_price)
    if ref_price >= Decimal(str(active.high_ticket_min_price)) and pct >= active.high_ticket_min_discount_pct:
        return True

    if price <= Decimal(str(active.symbolic_price_max)) and list_price >= Decimal(str(active.symbolic_list_min)):
        return True

    return False


def evaluate(
    snapshot: ProductSnapshot,
    rules: DealRules | None = None,
    history_median: Decimal | None = None,
) -> Deal | None:
    """Evalúa si un snapshot cumple los requisitos de oferta extrema, gama alta (>= $10k al 30%) o error de precio."""
    active_rules = rules or DealRules()

    if snapshot.price <= 0:
        return None
    if not snapshot.seller.is_official:
        return None
    if not snapshot.in_stock_postal_codes:
        return None

    types: set[DealType] = set()
    reasons: list[str] = []

    disc = snapshot.discount_pct
    ref_price = max(snapshot.price, snapshot.list_price or Decimal("0"))
    high_ticket_min = Decimal(str(active_rules.high_ticket_min_price))

    if disc is not None and disc > active_rules.min_discount_pct:
        types.add(DealType.DISCOUNT)
        reasons.append(f"Descuento extremo de {disc:.1f}% (> {active_rules.min_discount_pct:.0f}%)")
    elif (
        disc is not None
        and ref_price >= high_ticket_min
        and disc >= active_rules.high_ticket_min_discount_pct
    ):
        types.add(DealType.DISCOUNT)
        reasons.append(
            f"Oferta en gama >= ${high_ticket_min:,.0f} con {disc:.1f}% de descuento "
            f"(>= {active_rules.high_ticket_min_discount_pct:.0f}%)"
        )

    if history_median is not None and history_median > 0:
        threshold = history_median * Decimal(str(active_rules.price_error_ratio))
        if snapshot.price <= threshold:
            types.add(DealType.PRICE_ERROR)
            drop_pct = float((history_median - snapshot.price) / history_median * Decimal("100"))
            reasons.append(
                f"Posible error de precio: ${snapshot.price:,.2f} vs mediana histórica "
                f"${history_median:,.2f} (-{drop_pct:.1f}%)"
            )

    sym_max = Decimal(str(active_rules.symbolic_price_max))
    sym_min = Decimal(str(active_rules.symbolic_list_min))
    if (
        snapshot.price <= sym_max
        and snapshot.list_price is not None
        and snapshot.list_price >= sym_min
    ):
        types.add(DealType.PRICE_ERROR)
        reasons.append(
            f"Precio simbólico (${snapshot.price:,.2f} sobre lista de ${snapshot.list_price:,.2f})"
        )

    if not types:
        return None

    return Deal(
        snapshot=snapshot,
        types=types,
        reasons=reasons,
        history_median=history_median,
    )
