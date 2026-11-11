"""Cazador de vuelos baratos y errores de tarifa (Multi-portal Web + Travelpayouts API).

Investiga y monitorea vuelos baratos y errores de tarifa en:
  1. Portales web en México (con Playwright, sin requerir API key):
     - Viajes Suburbia (`https://viajes.suburbia.com.mx/vuelos/`)
     - Viajes Liverpool (`https://viajes.liverpool.com.mx/vuelos`)
     - Despegar México (`https://www.despegar.com.mx/vuelos/`)
     - BestDay (`https://www.bestday.com.mx/vuelos/`)
     - PriceTravel (`https://www.pricetravel.com.mx/vuelos`)
     - Volaris (`https://www.volaris.com/es/ofertas-de-vuelos`)
     - Viva Aerobus (`https://www.vivaaerobus.com/es-mx/promociones/ofertas-de-vuelos`)
     - Aeroméxico (`https://www.aeromexico.com/es-mx/promociones/vuelos`)
  2. API de Travelpayouts / Aviasales (`https://api.travelpayouts.com/aviasales/v3/prices_for_dates`)
     cuando se configura `TRAVELPAYOUTS_TOKEN`.

Criterios de alerta (configurables en `config.yaml` -> `flights`):
  * Caída fuerte frente al precio normal de la ruta (mediana histórica en SQLite, mediana
    de tarifas actuales o precio de lista/anterior tachado en el portal).
  * "Error de tarifa" si la caída es todavía mayor (por defecto >= 70%).
  * Tope absoluto para vuelos nacionales (ej. redondo nacional <= $1,400 MXN, sencillo <= $700 MXN).
  * Tope absoluto opcional por ruta (`max_price`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Any, Awaitable, Callable, Iterable, Protocol, Sequence
from urllib.parse import quote, urljoin, urlparse

import httpx
from playwright.async_api import BrowserContext, Page

from ofertas_monitor.repository import Repository
from ofertas_monitor.stores.base import parse_price

logger = logging.getLogger(__name__)

TRAVELPAYOUTS_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AVIASALES_ORIGIN = "https://www.aviasales.com"
HISTORY_NAMESPACE = "vuelo"

# Códigos IATA de ciudades/aeropuertos de México (para el tope "nacional")
MX_IATA: frozenset[str] = frozenset(
    {
        "MEX", "NLU", "TLC", "CUN", "GDL", "MTY", "TIJ", "SJD", "PVR", "MID", "OAX", "HUX",
        "ZIH", "ACA", "VER", "BJX", "CUL", "HMO", "CZM", "PXM", "TAM", "VSA", "CJS", "CUU",
        "AGU", "ZCL", "SLP", "TRC", "MZT", "LAP", "PBC", "QRO", "CME", "CPE", "CTM", "DGO",
        "MLM", "MXL", "LMM", "LTO", "NLD", "REX", "TAP", "TGZ", "UPN", "ZLO", "TQO", "CVM",
        "PQM", "PPE", "GYM", "IZT", "MAM", "CEN", "LZC", "TPQ", "CLQ",
    }
)

CITY_NAMES: dict[str, str] = {
    "MEX": "Ciudad de México", "NLU": "CDMX (AIFA)", "TLC": "Toluca", "PBC": "Puebla",
    "QRO": "Querétaro", "CUN": "Cancún", "GDL": "Guadalajara", "MTY": "Monterrey",
    "TIJ": "Tijuana", "SJD": "Los Cabos", "PVR": "Puerto Vallarta", "MID": "Mérida",
    "OAX": "Oaxaca", "HUX": "Huatulco", "ZIH": "Ixtapa-Zihuatanejo", "ACA": "Acapulco",
    "VER": "Veracruz", "BJX": "León/Bajío", "CUL": "Culiacán", "HMO": "Hermosillo",
    "CZM": "Cozumel", "PXM": "Puerto Escondido", "TQO": "Tulum", "MZT": "Mazatlán",
    "LAP": "La Paz", "CJS": "Ciudad Juárez", "CUU": "Chihuahua", "VSA": "Villahermosa",
    "TGZ": "Tuxtla Gutiérrez", "CPE": "Campeche", "CTM": "Chetumal", "AGU": "Aguascalientes",
    "SLP": "San Luis Potosí", "TRC": "Torreón", "ZCL": "Zacatecas", "MXL": "Mexicali",
    "LAX": "Los Ángeles", "NYC": "Nueva York", "JFK": "Nueva York (JFK)", "MIA": "Miami",
    "ORL": "Orlando", "MCO": "Orlando", "LAS": "Las Vegas", "CHI": "Chicago", "ORD": "Chicago",
    "HOU": "Houston", "IAH": "Houston", "DFW": "Dallas", "SFO": "San Francisco",
    "SAT": "San Antonio", "PHX": "Phoenix", "DEN": "Denver", "ATL": "Atlanta", "SEA": "Seattle",
    "YTO": "Toronto", "YYZ": "Toronto", "YVR": "Vancouver", "YMQ": "Montreal",
    "MAD": "Madrid", "BCN": "Barcelona", "PAR": "París", "CDG": "París", "LON": "Londres",
    "ROM": "Roma", "FCO": "Roma", "AMS": "Ámsterdam", "FRA": "Fráncfort", "IST": "Estambul",
    "TYO": "Tokio", "NRT": "Tokio", "SEL": "Seúl", "BOG": "Bogotá", "MDE": "Medellín",
    "CTG": "Cartagena", "LIM": "Lima", "SCL": "Santiago de Chile", "BUE": "Buenos Aires",
    "EZE": "Buenos Aires", "SAO": "São Paulo", "GRU": "São Paulo", "RIO": "Río de Janeiro",
    "HAV": "La Habana", "PTY": "Panamá", "SJO": "San José (CR)", "GUA": "Guatemala",
    "SAL": "San Salvador", "PUJ": "Punta Cana", "SDQ": "Santo Domingo", "UIO": "Quito",
}

AIRLINE_NAMES: dict[str, str] = {
    "Y4": "Volaris", "VB": "Viva Aerobus", "AM": "Aeroméxico", "AA": "American Airlines",
    "UA": "United", "DL": "Delta", "WN": "Southwest", "AS": "Alaska", "B6": "JetBlue",
    "NK": "Spirit", "F9": "Frontier", "AC": "Air Canada", "WS": "WestJet", "IB": "Iberia",
    "UX": "Air Europa", "BA": "British Airways", "AF": "Air France", "KL": "KLM",
    "LH": "Lufthansa", "TK": "Turkish Airlines", "EK": "Emirates", "QR": "Qatar Airways",
    "AV": "Avianca", "CM": "Copa", "LA": "LATAM", "AR": "Aerolíneas Argentinas",
    "JJ": "LATAM Brasil", "H2": "Sky Airline", "DM": "Arajet", "VW": "Aeromar",
    "NH": "ANA", "JL": "Japan Airlines", "KE": "Korean Air", "CX": "Cathay Pacific",
}


@dataclass(frozen=True)
class FlightPortalSpec:
    key: str
    label: str
    promo_url: str
    default_airline: str = ""
    route_url_template: str | None = None


FLIGHT_PORTALS: dict[str, FlightPortalSpec] = {
    "viajes_suburbia": FlightPortalSpec(
        key="viajes_suburbia",
        label="Viajes Suburbia",
        promo_url="https://viajes.suburbia.com.mx/vuelos/",
        route_url_template=(
            "https://viajes.suburbia.com.mx/shop/flights/results/roundtrip/"
            "{origin}/{destination}/{departure_date}/{return_date}/1/0/0"
        ),
    ),
    "viajes_liverpool": FlightPortalSpec(
        key="viajes_liverpool",
        label="Viajes Liverpool",
        promo_url="https://viajes.liverpool.com.mx/vuelos",
    ),
    "despegar": FlightPortalSpec(
        key="despegar",
        label="Despegar",
        promo_url="https://www.despegar.com.mx/vuelos/",
        route_url_template=(
            "https://www.despegar.com.mx/shop/flights/results/roundtrip/"
            "{origin}/{destination}/{departure_date}/{return_date}/1/0/0"
        ),
    ),
    "bestday": FlightPortalSpec(
        key="bestday",
        label="BestDay",
        promo_url="https://www.bestday.com.mx/vuelos/",
        route_url_template=(
            "https://www.bestday.com.mx/shop/flights/results/roundtrip/"
            "{origin}/{destination}/{departure_date}/{return_date}/1/0/0"
        ),
    ),
    "pricetravel": FlightPortalSpec(
        key="pricetravel",
        label="PriceTravel",
        promo_url="https://www.pricetravel.com.mx/vuelos",
    ),
    "volaris": FlightPortalSpec(
        key="volaris",
        label="Volaris",
        promo_url="https://www.volaris.com/es/ofertas-de-vuelos",
        default_airline="Y4",
    ),
    "vivaaerobus": FlightPortalSpec(
        key="vivaaerobus",
        label="Viva Aerobus",
        promo_url="https://www.vivaaerobus.com/es-mx/promociones/ofertas-de-vuelos",
        default_airline="VB",
    ),
    "aeromexico": FlightPortalSpec(
        key="aeromexico",
        label="Aeroméxico",
        promo_url="https://www.aeromexico.com/es-mx/promociones/vuelos",
        default_airline="AM",
    ),
}

DEFAULT_FLIGHT_PORTALS: tuple[str, ...] = tuple(FLIGHT_PORTALS.keys())

TRIP_ONEWAY = "oneway"
TRIP_ROUNDTRIP = "roundtrip"
ANY_DESTINATION = {"", "ANY", "CUALQUIERA", "*", "TODOS"}


class FlightApiError(RuntimeError):
    """Error al consultar una fuente de vuelos (sin exponer tokens)."""


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().strip()


_CITY_ALIASES: dict[str, str] = {
    "cdmx": "MEX",
    "ciudad de mexico": "MEX",
    "mexico df": "MEX",
    "distrito federal": "MEX",
    "aifa": "NLU",
    "santa lucia": "NLU",
    "puebla": "PBC",
    "queretaro": "QRO",
    "cancun": "CUN",
    "guadalajara": "GDL",
    "monterrey": "MTY",
    "tijuana": "TIJ",
    "los cabos": "SJD",
    "san jose del cabo": "SJD",
    "cabo san lucas": "SJD",
    "puerto vallarta": "PVR",
    "vallarta": "PVR",
    "merida": "MID",
    "oaxaca": "OAX",
    "huatulco": "HUX",
    "ixtapa": "ZIH",
    "zihuatanejo": "ZIH",
    "acapulco": "ACA",
    "veracruz": "VER",
    "leon": "BJX",
    "bajio": "BJX",
    "culiacan": "CUL",
    "hermosillo": "HMO",
    "cozumel": "CZM",
    "puerto escondido": "PXM",
    "tulum": "TQO",
    "mazatlan": "MZT",
    "la paz": "LAP",
    "ciudad juarez": "CJS",
    "chihuahua": "CUU",
    "villahermosa": "VSA",
    "tuxtla": "TGZ",
    "tuxtla gutierrez": "TGZ",
    "campeche": "CPE",
    "chetumal": "CTM",
    "aguascalientes": "AGU",
    "san luis potosi": "SLP",
    "torreon": "TRC",
    "zacatecas": "ZCL",
    "mexicali": "MXL",
    "los angeles": "LAX",
    "nueva york": "NYC",
    "new york": "NYC",
    "miami": "MIA",
    "orlando": "ORL",
    "las vegas": "LAS",
    "chicago": "CHI",
    "houston": "HOU",
    "dallas": "DFW",
    "san francisco": "SFO",
    "san antonio": "SAT",
    "phoenix": "PHX",
    "denver": "DEN",
    "atlanta": "ATL",
    "seattle": "SEA",
    "toronto": "YTO",
    "vancouver": "YVR",
    "montreal": "YMQ",
    "madrid": "MAD",
    "barcelona": "BCN",
    "paris": "PAR",
    "londres": "LON",
    "roma": "ROM",
    "amsterdam": "AMS",
    "francfort": "FRA",
    "estambul": "IST",
    "tokio": "TYO",
    "tokyo": "TYO",
    "seul": "SEL",
    "bogota": "BOG",
    "medellin": "MDE",
    "cartagena": "CTG",
    "lima": "LIM",
    "santiago": "SCL",
    "santiago de chile": "SCL",
    "buenos aires": "BUE",
    "sao paulo": "SAO",
    "rio de janeiro": "RIO",
    "la habana": "HAV",
    "panama": "PTY",
    "san jose": "SJO",
    "guatemala": "GUA",
    "san salvador": "SAL",
    "punta cana": "PUJ",
    "santo domingo": "SDQ",
    "quito": "UIO",
}
for _code, _cname in CITY_NAMES.items():
    _CITY_ALIASES.setdefault(_strip_accents(_cname), _code)


def resolve_iata(raw: str | None) -> str | None:
    """Convierte un nombre de ciudad o código IATA ('Cancún', 'CDMX', 'CUN') en su código IATA de 3 letras."""
    if not raw:
        return None
    cleaned = raw.strip()
    if len(cleaned) == 3 and cleaned.isalpha():
        up = cleaned.upper()
        if up in CITY_NAMES or up in MX_IATA:
            return up
    m_parens = re.search(r"\(([A-Za-z]{3})\)", cleaned)
    if m_parens:
        return m_parens.group(1).upper()
    norm = _strip_accents(cleaned)
    if norm in _CITY_ALIASES:
        return _CITY_ALIASES[norm]
    # Búsqueda por subcadena ordenada de nombre más largo a más corto
    for alias in sorted(_CITY_ALIASES, key=len, reverse=True):
        if len(alias) >= 4 and re.search(rf"\b{re.escape(alias)}\b", norm):
            return _CITY_ALIASES[alias]
    if len(cleaned) == 3 and cleaned.isalpha():
        return cleaned.upper()
    return None


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class FlightRoute:
    origin: str
    destination: str | None = None  # None = cualquier destino
    trips: tuple[str, ...] = (TRIP_ROUNDTRIP,)
    direct: bool = False
    max_price: Decimal | None = None  # tope absoluto opcional para esta ruta


@dataclass(frozen=True)
class FlightSettings:
    enabled: bool = False
    token: str | None = None
    currency: str = "mxn"
    market: str = "mx"
    months_ahead: int = 3
    min_drop_pct: float = 50.0
    error_fare_drop_pct: float = 70.0
    min_history_samples: int = 3
    min_batch_samples: int = 5
    national_max_price_oneway: Decimal | None = Decimal("700")
    national_max_price_roundtrip: Decimal | None = Decimal("1400")
    every_minutes: int = 60
    request_delay_seconds: float = 0.4
    routes: tuple[FlightRoute, ...] = ()
    portals: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        if not self.enabled:
            return False
        return bool(self.portals) or (bool(self.token) and bool(self.routes))


# --------------------------------------------------------------------------- modelos
@dataclass(frozen=True)
class FlightOffer:
    origin: str
    destination: str
    price: Decimal
    departure_at: str
    return_at: str | None
    airline: str = ""
    flight_number: str = ""
    transfers: int = 0
    return_transfers: int = 0
    origin_airport: str = ""
    destination_airport: str = ""
    link: str = ""
    currency: str = "MXN"
    list_price: Decimal | None = None
    source_portal: str = ""

    @property
    def is_roundtrip(self) -> bool:
        return bool(self.return_at)

    @property
    def trip(self) -> str:
        return TRIP_ROUNDTRIP if self.is_roundtrip else TRIP_ONEWAY

    @property
    def route_key(self) -> str:
        """Clave de la ruta (sin fechas) para el historial de precios."""
        return f"{self.origin}-{self.destination}-{self.trip}"

    @property
    def alert_key(self) -> str:
        """Clave única del vuelo concreto (con fechas) para no repetir alertas."""
        return f"{self.route_key}-{self.departure_date}-{self.return_date or ''}"

    @property
    def departure_date(self) -> str:
        return self.departure_at[:10]

    @property
    def return_date(self) -> str | None:
        return self.return_at[:10] if self.return_at else None

    @property
    def is_national(self) -> bool:
        o = self.origin_airport or self.origin
        d = self.destination_airport or self.destination
        return (self.origin in MX_IATA or o in MX_IATA) and (self.destination in MX_IATA or d in MX_IATA)

    @property
    def aviasales_url(self) -> str:
        if self.link.startswith("http"):
            return self.link
        if self.link:
            return f"{AVIASALES_ORIGIN}{self.link}"
        return self.google_flights_url

    @property
    def google_flights_url(self) -> str:
        query = f"Flights from {self.origin} to {self.destination} on {self.departure_date}"
        if self.return_date:
            query += f" through {self.return_date}"
        else:
            query += " one way"
        return f"https://www.google.com/travel/flights?q={quote(query)}&curr=MXN&hl=es-419"


@dataclass
class FlightDeal:
    offer: FlightOffer
    reference_price: Decimal | None
    reference_source: str | None
    is_error_fare: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def drop_pct(self) -> float | None:
        if self.reference_price is None or self.reference_price <= 0:
            return None
        return round(
            float((self.reference_price - self.offer.price) / self.reference_price * Decimal("100")), 1
        )


@dataclass
class FlightRunReport:
    offers: list[FlightOffer] = field(default_factory=list)
    deals: list[FlightDeal] = field(default_factory=list)
    notified: list[FlightDeal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False


# --------------------------------------------------------------------------- cliente API Travelpayouts
def _parse_offer(raw: dict[str, Any], currency: str) -> FlightOffer | None:
    try:
        price = Decimal(str(raw["price"]))
    except (KeyError, ValueError, ArithmeticError):
        return None
    origin = str(raw.get("origin") or "").upper()
    destination = str(raw.get("destination") or "").upper()
    departure = str(raw.get("departure_at") or "")
    if not origin or not destination or not departure or price <= 0:
        return None
    return FlightOffer(
        origin=origin,
        destination=destination,
        price=price,
        departure_at=departure,
        return_at=str(raw["return_at"]) if raw.get("return_at") else None,
        airline=str(raw.get("airline") or ""),
        flight_number=str(raw.get("flight_number") or ""),
        transfers=int(raw.get("transfers") or 0),
        return_transfers=int(raw.get("return_transfers") or 0),
        origin_airport=str(raw.get("origin_airport") or "").upper(),
        destination_airport=str(raw.get("destination_airport") or "").upper(),
        link=str(raw.get("link") or ""),
        currency=currency.upper(),
    )


class TravelpayoutsClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        currency: str = "mxn",
        market: str = "mx",
    ) -> None:
        self._token = token
        self._client = client
        self.currency = currency.lower()
        self.market = market

    async def prices_for_dates(
        self,
        *,
        origin: str,
        destination: str | None,
        departure_month: str,
        roundtrip: bool,
        direct: bool = False,
    ) -> list[FlightOffer]:
        params: dict[str, str] = {
            "origin": origin,
            "departure_at": departure_month,
            "one_way": "false" if roundtrip else "true",
            "direct": "true" if direct else "false",
            "currency": self.currency,
            "market": self.market,
            "sorting": "price",
            "limit": "100" if destination is None else "31",
            "unique": "true" if destination is None else "false",
        }
        if destination:
            params["destination"] = destination

        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            try:
                resp = await client.get(
                    TRAVELPAYOUTS_URL,
                    params=params,
                    headers={"X-Access-Token": self._token, "Accept-Encoding": "gzip, deflate"},
                )
            except httpx.HTTPError as exc:
                raise FlightApiError("Fallo de red al consultar Travelpayouts") from exc
        finally:
            if owns:
                await client.aclose()

        if resp.status_code in (401, 403):
            raise FlightApiError("Travelpayouts rechazó el token (revisa TRAVELPAYOUTS_TOKEN).")
        if resp.status_code == 429:
            raise FlightApiError("Travelpayouts: límite de peticiones alcanzado (HTTP 429).")
        if resp.status_code != 200:
            raise FlightApiError(f"Travelpayouts respondió HTTP {resp.status_code}.")
        try:
            body = resp.json()
        except ValueError as exc:
            raise FlightApiError("Respuesta inválida de Travelpayouts.") from exc
        if not isinstance(body, dict) or not body.get("success", True):
            raise FlightApiError(f"Travelpayouts devolvió error: {body.get('error') if isinstance(body, dict) else body}")

        currency = str(body.get("currency") or self.currency)
        offers: list[FlightOffer] = []
        for raw in body.get("data") or []:
            if isinstance(raw, dict):
                offer = _parse_offer(raw, currency)
                if offer is not None:
                    offers.append(offer)
        return offers


# --------------------------------------------------------------------------- Scraper Web Multi-Portal (Playwright)
_URL_ROUTE_RE = re.compile(
    r"/(?:roundtrip|oneway|vuelos)/([A-Za-z]{3})/([A-Za-z]{3})(?:/(\d{4}-\d{2}-\d{2}))?(?:/(\d{4}-\d{2}-\d{2}))?",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TEXT_ROUTE_PATTERNS = (
    re.compile(r"(?:de|desde|saliendo de)\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]{3,25}?)\s+(?:a|hacia|rumbo a|->|→|-)\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]{3,25})", re.IGNORECASE),
    re.compile(r"([A-Za-záéíóúñÁÉÍÓÚÑ\s]{3,22}?)\s*(?:->|→|—|–|\ba\b)\s*([A-Za-záéíóúñÁÉÍÓÚÑ\s]{3,22})", re.IGNORECASE),
    re.compile(r"vuelos?\s+(?:baratos?\s+)?a\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]{3,25})", re.IGNORECASE),
)

_CARD_SELECTOR = (
    "[data-flight-offer], [data-origin][data-destination], "
    ".flight-card, .offer-card, .cluster-container, .deal-card, .fare-card, .promo-card"
)
_PRICE_SELECTOR = (
    "[data-testid='price'], .offer-price, .fare-price, .pricebox-big-text, "
    ".a-card-discount, .pdp-price-current, .price-current, .price-promo, .amount, .price"
)
_LIST_PRICE_SELECTOR = (
    "[data-testid='list-price'], .old-price, .price-before, .a-card-price, "
    ".pdp-price-before, .price-regular, s, del"
)


def _extract_route_from_text_or_url(text: str, href: str) -> tuple[str | None, str | None, str | None, str | None]:
    m_url = _URL_ROUTE_RE.search(href)
    if m_url:
        o = m_url.group(1).upper()
        d = m_url.group(2).upper()
        return o, d, m_url.group(3), m_url.group(4)

    for idx, pat in enumerate(_TEXT_ROUTE_PATTERNS):
        for m in pat.finditer(text):
            if idx == 2:
                dest = resolve_iata(m.group(1))
                if dest and dest != "MEX":
                    return "MEX", dest, None, None
            else:
                orig = resolve_iata(m.group(1))
                dest = resolve_iata(m.group(2))
                if orig and dest and orig != dest:
                    return orig, dest, None, None
    return None, None, None, None


class FlightPortalScraper:
    """Extrae ofertas de vuelos desde Viajes Suburbia, Despegar, BestDay, Liverpool, PriceTravel y aerolíneas."""

    def __init__(self, *, today: date | None = None) -> None:
        self._today = today

    def _default_dates(self, roundtrip: bool) -> tuple[str, str | None]:
        base = self._today or datetime.now(timezone.utc).date()
        dep = (base + timedelta(days=30)).isoformat()
        ret = (base + timedelta(days=37)).isoformat() if roundtrip else None
        return dep, ret

    async def scrape_portal(
        self,
        context: BrowserContext,
        portal_key: str,
        *,
        routes: Sequence[FlightRoute] = (),
    ) -> list[FlightOffer]:
        spec = FLIGHT_PORTALS.get(portal_key.lower())
        if spec is None:
            return []

        urls_to_visit: list[tuple[str, str | None, str | None]] = [(spec.promo_url, None, None)]
        if spec.route_url_template and routes:
            dep_default, ret_default = self._default_dates(roundtrip=True)
            for r in routes[:4]:
                if r.destination:
                    url = spec.route_url_template.format(
                        origin=r.origin,
                        destination=r.destination,
                        departure_date=dep_default,
                        return_date=ret_default or dep_default,
                    )
                    urls_to_visit.append((url, r.origin, r.destination))

        offers: list[FlightOffer] = []
        page: Page = await context.new_page()
        try:
            for url, fallback_origin, fallback_dest in urls_to_visit:
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    if resp is not None and resp.status in (403, 429):
                        logger.warning("Portal de vuelos %s respondió HTTP %s", spec.label, resp.status)
                        break
                    page_offers = await self._extract_from_page(
                        page,
                        spec,
                        page_url=url,
                        fallback_origin=fallback_origin,
                        fallback_dest=fallback_dest,
                    )
                    offers.extend(page_offers)
                except Exception as exc:
                    logger.warning("Error leyendo portal de vuelos %s (%s): %s", spec.label, url, exc)
        finally:
            await page.close()
        return offers

    async def _extract_from_page(
        self,
        page: Page,
        spec: FlightPortalSpec,
        *,
        page_url: str,
        fallback_origin: str | None = None,
        fallback_dest: str | None = None,
    ) -> list[FlightOffer]:
        parsed_origin = urlparse(page_url)
        base_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        offers: list[FlightOffer] = []

        cards = page.locator(_CARD_SELECTOR)
        count = min(await cards.count(), 40)
        for idx in range(count):
            card = cards.nth(idx)
            attr_orig = resolve_iata(await card.get_attribute("data-origin"))
            attr_dest = resolve_iata(await card.get_attribute("data-destination"))
            attr_price = parse_price(await card.get_attribute("data-price"))
            attr_list = parse_price(
                await card.get_attribute("data-list-price")
                or await card.get_attribute("data-original-price")
            )
            attr_dep = await card.get_attribute("data-departure")
            attr_ret = await card.get_attribute("data-return")
            attr_trip = (await card.get_attribute("data-trip") or "").lower()
            attr_airline = (await card.get_attribute("data-airline") or spec.default_airline).upper()

            link_loc = card.locator("a[href]")
            href = ""
            if await link_loc.count() > 0:
                href = (await link_loc.first.get_attribute("href") or "").strip()
            elif await card.evaluate("el => el.tagName.toLowerCase() === 'a'"):
                href = (await card.get_attribute("href") or "").strip()
            full_link = urljoin(base_origin, href) if href else page_url

            price = attr_price
            if price is None:
                p_loc = card.locator(_PRICE_SELECTOR)
                if await p_loc.count() > 0:
                    price = parse_price(await p_loc.first.inner_text())
            if price is None or price <= 0:
                continue

            list_price = attr_list
            if list_price is None:
                lp_loc = card.locator(_LIST_PRICE_SELECTOR)
                if await lp_loc.count() > 0:
                    list_price = parse_price(await lp_loc.first.inner_text())
            if list_price is not None and list_price <= price:
                list_price = None

            card_text = (await card.inner_text()).strip()
            txt_orig, txt_dest, url_dep, url_ret = _extract_route_from_text_or_url(card_text, full_link)
            origin = attr_orig or txt_orig or fallback_origin
            destination = attr_dest or txt_dest or fallback_dest
            if not origin or not destination or origin == destination:
                continue

            lower_text = card_text.lower()
            is_oneway = (
                attr_trip in ("oneway", "sencillo")
                or "sencillo" in lower_text
                or "solo ida" in lower_text
                or "one way" in lower_text
            )
            roundtrip = not is_oneway
            default_dep, default_ret = self._default_dates(roundtrip=roundtrip)

            dates_in_text = _DATE_RE.findall(card_text)
            dep_date = (
                attr_dep
                or url_dep
                or (dates_in_text[0] if dates_in_text else default_dep)
            )
            ret_date = (
                attr_ret
                or url_ret
                or (dates_in_text[1] if len(dates_in_text) > 1 else (default_ret if roundtrip else None))
            )
            if is_oneway:
                ret_date = None

            offers.append(
                FlightOffer(
                    origin=origin,
                    destination=destination,
                    price=price,
                    departure_at=dep_date,
                    return_at=ret_date,
                    airline=attr_airline,
                    link=full_link,
                    currency="MXN",
                    list_price=list_price,
                    source_portal=spec.label,
                )
            )

        return offers


# --------------------------------------------------------------------------- evaluación
def upcoming_months(today: date, count: int) -> list[str]:
    months: list[str] = []
    y, m = today.year, today.month
    for _ in range(max(1, count)):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def evaluate_flight(
    offer: FlightOffer,
    settings: FlightSettings,
    *,
    history_median: Decimal | None,
    batch_median: Decimal | None,
    route_max_price: Decimal | None = None,
) -> FlightDeal | None:
    reasons: list[str] = []
    if history_median is not None:
        reference, source = history_median, "historial 30 días"
    elif batch_median is not None:
        reference, source = batch_median, "tarifas actuales de la ruta"
    elif offer.list_price is not None and offer.list_price > offer.price:
        portal_lbl = f" en {offer.source_portal}" if offer.source_portal else ""
        reference, source = offer.list_price, f"precio regular{portal_lbl}"
    else:
        reference, source = None, None

    is_error = False
    hit = False

    if reference is not None and reference > 0:
        drop = float((reference - offer.price) / reference * Decimal("100"))
        if drop >= settings.error_fare_drop_pct:
            hit = is_error = True
            reasons.append(f"Posible ERROR DE TARIFA: {drop:.0f}% abajo del precio normal (${reference:,.0f})")
        elif drop >= settings.min_drop_pct:
            hit = True
            reasons.append(f"{drop:.0f}% abajo del precio normal de la ruta (${reference:,.0f})")

    if offer.is_national:
        cap = settings.national_max_price_roundtrip if offer.is_roundtrip else settings.national_max_price_oneway
        if cap is not None and offer.price <= cap:
            hit = True
            tipo = "redondo" if offer.is_roundtrip else "sencillo"
            reasons.append(f"Vuelo nacional {tipo} por debajo de ${cap:,.0f}")

    if route_max_price is not None and offer.price <= route_max_price:
        hit = True
        reasons.append(f"Debajo de tu tope para esta ruta (${route_max_price:,.0f})")

    if not hit:
        return None
    return FlightDeal(
        offer=offer,
        reference_price=reference,
        reference_source=source if reference is not None else None,
        is_error_fare=is_error,
        reasons=reasons,
    )


def _batch_medians(offers: Iterable[FlightOffer], min_samples: int) -> dict[str, Decimal]:
    groups: dict[str, list[Decimal]] = {}
    for o in offers:
        groups.setdefault(o.route_key, []).append(o.price)
    return {
        key: Decimal(str(median(prices)))
        for key, prices in groups.items()
        if len(prices) >= min_samples
    }


# --------------------------------------------------------------------------- orquestador
class FlightNotifier(Protocol):
    async def send_flight_deal(self, deal: FlightDeal) -> None: ...


class FlightMonitor:
    def __init__(
        self,
        *,
        settings: FlightSettings,
        repository: Repository,
        notifier: FlightNotifier | None,
        client: TravelpayoutsClient | None = None,
        portal_scraper: FlightPortalScraper | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.notifier = notifier
        self.client = client or (
            TravelpayoutsClient(
                settings.token, currency=settings.currency, market=settings.market
            )
            if settings.token
            else None
        )
        self.portal_scraper = portal_scraper
        self._clock = clock
        self._sleep = sleep

    def _is_due(self, now: datetime) -> bool:
        last = self.repository.get_meta_datetime("flights_last_run")
        if last is None:
            return True
        return (now - last).total_seconds() >= self.settings.every_minutes * 60 - 30

    async def run_once(
        self,
        *,
        context: BrowserContext | None = None,
        force: bool = False,
    ) -> FlightRunReport:
        report = FlightRunReport()
        now = self._clock()
        if not self.settings.active:
            report.skipped = True
            return report
        if not force and not self._is_due(now):
            report.skipped = True
            logger.info("Vuelos: aún no toca (cada %d min).", self.settings.every_minutes)
            return report

        route_caps: dict[str, Decimal] = {}
        route_pair_caps: dict[tuple[str, str], Decimal] = {}
        for r in self.settings.routes:
            if r.destination and r.max_price is not None:
                route_pair_caps[(r.origin, r.destination)] = r.max_price

        # 1) Portales web (Viajes Suburbia, Despegar, BestDay, Viajes Liverpool, PriceTravel, Volaris, Viva, Aeroméxico)
        if context is not None and self.settings.portals:
            scraper = self.portal_scraper or FlightPortalScraper(today=now.date())
            for portal_key in self.settings.portals:
                portal_offers = await scraper.scrape_portal(
                    context, portal_key, routes=self.settings.routes
                )
                for o in portal_offers:
                    cap = route_pair_caps.get((o.origin, o.destination))
                    if cap is not None:
                        route_caps[o.alert_key] = cap
                report.offers.extend(portal_offers)

        # 2) API Travelpayouts (si hay token o cliente inyectado)
        if self.client is not None and self.settings.routes:
            months = upcoming_months(now.date(), self.settings.months_ahead)
            first = True
            for route in self.settings.routes:
                for trip in route.trips:
                    for month in months:
                        if not first and self.settings.request_delay_seconds > 0:
                            await self._sleep(self.settings.request_delay_seconds)
                        first = False
                        try:
                            offers = await self.client.prices_for_dates(
                                origin=route.origin,
                                destination=route.destination,
                                departure_month=month,
                                roundtrip=trip == TRIP_ROUNDTRIP,
                                direct=route.direct,
                            )
                        except FlightApiError as exc:
                            logger.warning("Vuelos %s->%s %s: %s", route.origin, route.destination or "*", month, exc)
                            report.errors.append(str(exc))
                            if "token" in str(exc).lower():
                                break
                            continue
                        for o in offers:
                            if route.max_price is not None:
                                route_caps[o.alert_key] = route.max_price
                        report.offers.extend(offers)

        # Quitar duplicados (mismo vuelo devuelto por fuentes distintas): queda el más barato
        unique: dict[str, FlightOffer] = {}
        for o in report.offers:
            prev = unique.get(o.alert_key)
            if prev is None or o.price < prev.price:
                unique[o.alert_key] = o
        offers = list(unique.values())
        batch = _batch_medians(offers, self.settings.min_batch_samples)

        # Historial: se evalúa ANTES de guardar las tarifas nuevas, para no contaminar la referencia
        history: dict[str, Decimal | None] = {}
        for key in {o.route_key for o in offers}:
            history[key] = self.repository.median_value(
                HISTORY_NAMESPACE, key, min_samples=self.settings.min_history_samples, now=now
            )

        for o in sorted(offers, key=lambda x: x.price):
            deal = evaluate_flight(
                o,
                self.settings,
                history_median=history.get(o.route_key),
                batch_median=batch.get(o.route_key),
                route_max_price=route_caps.get(o.alert_key),
            )
            if deal is None:
                continue
            report.deals.append(deal)
            if not self.repository.should_notify_key(HISTORY_NAMESPACE, o.alert_key, o.price, now=now):
                continue
            if self.notifier is not None:
                try:
                    await self.notifier.send_flight_deal(deal)
                except Exception as exc:
                    logger.error("No se pudo enviar alerta de vuelo: %s", exc)
                    report.errors.append(f"Telegram: {exc}")
                    continue
            self.repository.mark_notified_key(HISTORY_NAMESPACE, o.alert_key, o.price, now=now)
            report.notified.append(deal)

        # Guardar el precio más barato por ruta en esta corrida (1 muestra por ruta por corrida)
        cheapest: dict[str, Decimal] = {}
        for o in offers:
            if o.route_key not in cheapest or o.price < cheapest[o.route_key]:
                cheapest[o.route_key] = o.price
        for key, price in cheapest.items():
            self.repository.record_value(HISTORY_NAMESPACE, key, price, now)

        self.repository.set_meta_datetime("flights_last_run", now)
        return report


def city_label(code: str) -> str:
    name = CITY_NAMES.get(code.upper())
    return f"{name} ({code})" if name else code


def airline_label(code: str) -> str:
    return AIRLINE_NAMES.get(code.upper(), code) if code else "—"


def parse_flight_portals(raw: Sequence[Any] | None) -> tuple[str, ...]:
    """Valida la lista `flights.portals` del YAML."""
    from ofertas_monitor.config import ConfigError

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("flights.portals debe ser una lista.")
    portals: list[str] = []
    for item in raw:
        key = str(item).strip().lower()
        if key not in FLIGHT_PORTALS:
            valid = ", ".join(FLIGHT_PORTALS.keys())
            raise ConfigError(f"Portal de vuelos no soportado: {item!r} (válidos: {valid}).")
        if key not in portals:
            portals.append(key)
    return tuple(portals)


def parse_flight_routes(raw: Sequence[Any] | None) -> tuple[FlightRoute, ...]:
    """Convierte la lista `flights.routes` del YAML en FlightRoute (valida códigos IATA)."""
    from ofertas_monitor.config import ConfigError  # import local: evita ciclo

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("flights.routes debe ser una lista.")
    routes: list[FlightRoute] = []
    for entry in raw:
        if not isinstance(entry, dict) or "origin" not in entry:
            raise ConfigError("Cada ruta de vuelo debe incluir al menos 'origin'.")
        origin = str(entry["origin"]).strip().upper()
        if len(origin) != 3 or not origin.isalpha():
            raise ConfigError(f"Código IATA de origen inválido: {entry['origin']!r}.")
        dest_raw = str(entry.get("destination") or "").strip().upper()
        destination: str | None
        if dest_raw in ANY_DESTINATION:
            destination = None
        elif len(dest_raw) == 3 and dest_raw.isalpha():
            destination = dest_raw
        else:
            raise ConfigError(f"Código IATA de destino inválido: {entry.get('destination')!r}.")
        trip_raw = str(entry.get("trip") or "roundtrip").strip().lower()
        trip_map = {
            "roundtrip": (TRIP_ROUNDTRIP,), "redondo": (TRIP_ROUNDTRIP,),
            "oneway": (TRIP_ONEWAY,), "sencillo": (TRIP_ONEWAY,),
            "both": (TRIP_ROUNDTRIP, TRIP_ONEWAY), "ambos": (TRIP_ROUNDTRIP, TRIP_ONEWAY),
        }
        if trip_raw not in trip_map:
            raise ConfigError(f"trip inválido {trip_raw!r} (usa redondo, sencillo o ambos).")
        max_price = entry.get("max_price")
        routes.append(
            FlightRoute(
                origin=origin,
                destination=destination,
                trips=trip_map[trip_raw],
                direct=bool(entry.get("direct", False)),
                max_price=Decimal(str(max_price)) if max_price not in (None, "", 0) else None,
            )
        )
    return tuple(routes)
