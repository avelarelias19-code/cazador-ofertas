"""Adaptadores de tiendas."""
from ofertas_monitor.stores.amazon import AmazonScraper
from ofertas_monitor.stores.base import BlockedError, ExtractionError, StoreScraper, parse_price
from ofertas_monitor.stores.mercadolibre import MercadoLibreScraper, extract_item_id
from ofertas_monitor.stores.retail import (
    CoppelScraper,
    LaComerScraper,
    OfficeDepotScraper,
    SuburbiaScraper,
)
from ofertas_monitor.stores.vtex import (
    BodegasAlianzaScraper,
    ChedrauiScraper,
    ElektraScraper,
    VtexScraper,
)
from ofertas_monitor.stores.walmart import WalmartScraper

__all__ = [
    "AmazonScraper",
    "BlockedError",
    "BodegasAlianzaScraper",
    "ChedrauiScraper",
    "CoppelScraper",
    "ElektraScraper",
    "ExtractionError",
    "LaComerScraper",
    "MercadoLibreScraper",
    "OfficeDepotScraper",
    "StoreScraper",
    "SuburbiaScraper",
    "VtexScraper",
    "WalmartScraper",
    "extract_item_id",
    "parse_price",
]

