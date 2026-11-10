"""Filtro estricto de vendedor oficial (descarta terceros/marketplace)."""
from __future__ import annotations

import re
import unicodedata

from ofertas_monitor.models import Store

# Merchant ID oficial de Amazon.com.mx (Retail directo)
AMAZON_MX_MERCHANT_ID = "AVDBXBAVVSXLQ"

_SOLD_BY_PREFIX_RE = re.compile(
    r"^(?:vendido\s+y\s+enviado\s+por|vendido\s+por|enviado\s+por|sold\s+by)\s+",
    re.IGNORECASE,
)

_OFFICIAL_NAMES: dict[Store, set[str]] = {
    Store.AMAZON: {"amazon mexico", "amazon.com.mx", "amazon"},
    Store.WALMART: {"walmart", "walmart mexico", "walmart de mexico", "walmart.com.mx"},
    Store.CHEDRAUI: {"chedraui", "tiendas chedraui", "chedraui.com.mx"},
    Store.ELEKTRA: {"elektra", "tiendas elektra", "elektra.mx", "elektra.com.mx"},
    Store.BODEGAS_ALIANZA: {"bodegas alianza", "alianza", "bodegasalianza.com"},
    Store.OFFICE_DEPOT: {"office depot", "office depot mexico", "officedepot.com.mx"},
    Store.LA_COMER: {"la comer", "lacomer", "lacomer.com.mx", "fresko", "city market"},
    Store.SUBURBIA: {"suburbia", "suburbia.com.mx", "tiendas suburbia"},
    Store.COPPEL: {"coppel", "coppel.com", "tiendas coppel"},
}


def normalize_name(raw: str | None) -> str:
    """Normaliza un nombre de vendedor: minúsculas, sin acentos ni prefijos."""
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    collapsed = re.sub(r"\s+", " ", without_accents).strip(" .,;:-")
    cleaned = _SOLD_BY_PREFIX_RE.sub("", collapsed)
    return cleaned.strip(" .,;:-").lower()


def is_official_seller(
    store: Store,
    seller_name: str | None,
    seller_id: str | None = None,
    *,
    official_store_id: int | str | None = None,
) -> bool:
    """Determina si el vendedor corresponde a la tienda oficial y no a un intermediario."""
    sid = seller_id.strip() if seller_id is not None else ""

    if store is Store.MERCADOLIBRE:
        if official_store_id is None:
            return False
        return str(official_store_id).strip() != ""

    if store is Store.AMAZON:
        if sid:
            return sid.upper() == AMAZON_MX_MERCHANT_ID
        return normalize_name(seller_name) in _OFFICIAL_NAMES[Store.AMAZON]

    if store is Store.WALMART:
        if sid and sid != "0":
            return False
        return normalize_name(seller_name) in _OFFICIAL_NAMES[Store.WALMART]

    if store in (Store.CHEDRAUI, Store.ELEKTRA, Store.BODEGAS_ALIANZA):
        # En VTEX, el seller "1" es siempre la tienda principal (1P); otros IDs son marketplace.
        if sid:
            return sid == "1"
        return normalize_name(seller_name) in _OFFICIAL_NAMES[store]

    if store in (Store.OFFICE_DEPOT, Store.LA_COMER, Store.SUBURBIA, Store.COPPEL):
        if sid and sid not in {"0", "1"}:
            return False
        return normalize_name(seller_name) in _OFFICIAL_NAMES[store]

    return False

