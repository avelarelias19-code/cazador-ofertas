"""Carga y validación de configuración YAML y variables de entorno."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

import yaml

from ofertas_monitor.detection import DealRules
from ofertas_monitor.flights import FlightSettings, parse_flight_portals, parse_flight_routes
from ofertas_monitor.models import Store

# Zona horaria de Ciudad de México (UTC-6 estándar)
MX_TZ = timezone(timedelta(hours=-6))

# 5 códigos postales clave de CDMX (Centro, Iztapalapa, Sur), Puebla y Querétaro
DEFAULT_POSTAL_CODES: tuple[str, ...] = ("06600", "09000", "04500", "72000", "76000")

# En México los códigos postales válidos van de 01000 a 99999 (00xxx no existe en SEPOMEX)
_MX_CP_RE = re.compile(r"^(?:0[1-9]|[1-9]\d)\d{3}$")


class ConfigError(ValueError):
    """Error de validación en config.yaml o variables de entorno."""


@dataclass(frozen=True)
class Target:
    store: Store
    url: str


@dataclass(frozen=True)
class SearchQuery:
    store: Store
    query: str
    max_results: int = 20


@dataclass(frozen=True)
class Settings:
    postal_codes: tuple[str, ...] = DEFAULT_POSTAL_CODES
    rules: DealRules = field(default_factory=DealRules)
    interval_minutes: int = 10
    day_interval_minutes: int = 10
    night_interval_minutes: int = 20
    day_start_hour: int = 6
    day_end_hour: int = 23
    min_delay_seconds: float = 4.0
    block_backoff_minutes: int = 60
    block_notice_cooldown_hours: int = 24
    queries_per_run: int = 0  # 0 = todas las búsquedas en cada corrida
    db_path: str = "ofertas.db"
    headless: bool = True
    targets: list[Target] = field(default_factory=list)
    queries: list[SearchQuery] = field(default_factory=list)
    flights: FlightSettings = field(default_factory=FlightSettings)
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    ml_access_token: str | None = None


def select_query_batch(
    queries: list[SearchQuery], batch_size: int, batch_index: int
) -> tuple[list[SearchQuery], int]:
    """Devuelve el lote de búsquedas de esta corrida y el índice del siguiente lote.

    Rota por lotes para que cada corrida sea corta (cabe en GitHub Actions) y en unas
    cuantas corridas se cubran todas las búsquedas.
    """
    if batch_size <= 0 or batch_size >= len(queries):
        return list(queries), 0
    n_batches = -(-len(queries) // batch_size)
    idx = batch_index % n_batches
    start = idx * batch_size
    return list(queries[start : start + batch_size]), (idx + 1) % n_batches


def _parse_flights(raw: object, environ: Mapping[str, str]) -> FlightSettings:
    token = environ.get("TRAVELPAYOUTS_TOKEN") or None
    if raw is None:
        return FlightSettings(token=token)
    if not isinstance(raw, dict):
        raise ConfigError("La sección 'flights' debe ser un mapeo.")
    d = FlightSettings()

    def _opt_dec(key: str, default: Decimal | None) -> Decimal | None:
        if key not in raw:
            return default
        val = raw.get(key)
        return Decimal(str(val)) if val not in (None, "", 0) else None

    min_drop = float(raw.get("min_drop_pct", d.min_drop_pct))
    err_drop = float(raw.get("error_fare_drop_pct", d.error_fare_drop_pct))
    if not (0 < min_drop < 100) or not (0 < err_drop < 100):
        raise ConfigError("flights.min_drop_pct y error_fare_drop_pct deben estar entre 0 y 100.")
    return FlightSettings(
        enabled=bool(raw.get("enabled", True)),
        token=token,
        currency=str(raw.get("currency", d.currency)).lower(),
        market=str(raw.get("market", d.market)).lower(),
        months_ahead=int(raw.get("months_ahead", d.months_ahead)),
        min_drop_pct=min_drop,
        error_fare_drop_pct=err_drop,
        min_history_samples=int(raw.get("min_history_samples", d.min_history_samples)),
        min_batch_samples=int(raw.get("min_batch_samples", d.min_batch_samples)),
        national_max_price_oneway=_opt_dec("national_max_price_oneway", d.national_max_price_oneway),
        national_max_price_roundtrip=_opt_dec("national_max_price_roundtrip", d.national_max_price_roundtrip),
        every_minutes=int(raw.get("every_minutes", d.every_minutes)),
        request_delay_seconds=float(raw.get("request_delay_seconds", d.request_delay_seconds)),
        routes=parse_flight_routes(raw.get("routes")),
        portals=parse_flight_portals(raw.get("portals")),
    )


def current_interval_minutes(settings: Settings, now: datetime | None = None) -> int:
    """Devuelve el intervalo en minutos según la hora local de CDMX (06:00-23:00 -> día, 23:00-06:00 -> noche)."""
    ref = (now or datetime.now(MX_TZ)).astimezone(MX_TZ)
    hour = ref.hour
    if settings.day_start_hour <= hour < settings.day_end_hour:
        return settings.day_interval_minutes
    return settings.night_interval_minutes


def _validate_postal_codes(raw_cps: object) -> tuple[str, ...]:
    if not isinstance(raw_cps, (list, tuple)):
        raise ConfigError("postal_codes debe ser una lista de 5 códigos postales.")
    if len(raw_cps) != 5:
        raise ConfigError(f"Se requieren exactamente 5 códigos postales, se recibieron {len(raw_cps)}.")

    validated: list[str] = []
    for cp in raw_cps:
        if not isinstance(cp, str):
            raise ConfigError(
                f"Cada código postal debe ser string entre comillas (recibido {cp!r} de tipo {type(cp).__name__})."
            )
        cleaned = cp.strip()
        if not _MX_CP_RE.match(cleaned):
            raise ConfigError(f"Código postal de México inválido: {cp!r}.")
        validated.append(cleaned)

    if len(set(validated)) != 5:
        raise ConfigError("Los 5 códigos postales no deben contener duplicados.")

    return tuple(validated)


def _parse_rules(raw: object) -> DealRules:
    if raw is None:
        return DealRules()
    if not isinstance(raw, dict):
        raise ConfigError("La sección 'rules' debe ser un mapeo.")

    defaults = DealRules()
    min_discount = float(raw.get("min_discount_pct", defaults.min_discount_pct))
    if not (0 < min_discount < 100):
        raise ConfigError("rules.min_discount_pct debe estar entre 0 y 100.")

    high_ticket_discount = float(
        raw.get("high_ticket_min_discount_pct", defaults.high_ticket_min_discount_pct)
    )
    if not (0 < high_ticket_discount < 100):
        raise ConfigError("rules.high_ticket_min_discount_pct debe estar entre 0 y 100.")

    ratio = float(raw.get("price_error_ratio", defaults.price_error_ratio))
    if not (0 < ratio < 1):
        raise ConfigError("rules.price_error_ratio debe estar entre 0 y 1.")

    return DealRules(
        min_discount_pct=min_discount,
        high_ticket_min_price=Decimal(
            str(raw.get("high_ticket_min_price", defaults.high_ticket_min_price))
        ),
        high_ticket_min_discount_pct=high_ticket_discount,
        price_error_ratio=ratio,
        min_history_samples=int(raw.get("min_history_samples", defaults.min_history_samples)),
        symbolic_price_max=Decimal(str(raw.get("symbolic_price_max", defaults.symbolic_price_max))),
        symbolic_list_min=Decimal(str(raw.get("symbolic_list_min", defaults.symbolic_list_min))),
    )


def _parse_targets(raw: object) -> list[Target]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("La sección 'targets' debe ser una lista.")

    targets: list[Target] = []
    for entry in raw:
        if not isinstance(entry, dict) or "store" not in entry or "url" not in entry:
            raise ConfigError("Cada target debe incluir 'store' y 'url'.")
        store_raw = str(entry["store"]).strip().lower()
        try:
            store = Store(store_raw)
        except ValueError as exc:
            raise ConfigError(f"Tienda no soportada: {entry['store']!r}.") from exc

        url = str(entry["url"]).strip()
        if not url.startswith("https://"):
            raise ConfigError(f"La URL del target debe iniciar con https:// ({url!r}).")
        targets.append(Target(store=store, url=url))

    return targets


def _parse_queries(raw: object) -> list[SearchQuery]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("La sección 'queries' debe ser una lista.")

    queries: list[SearchQuery] = []
    for entry in raw:
        if not isinstance(entry, dict) or "store" not in entry or "query" not in entry:
            raise ConfigError("Cada búsqueda en 'queries' debe incluir 'store' y 'query'.")
        store_raw = str(entry["store"]).strip().lower()
        try:
            store = Store(store_raw)
        except ValueError as exc:
            raise ConfigError(f"Tienda no soportada en queries: {entry['store']!r}.") from exc

        q = str(entry["query"]).strip()
        if not q:
            raise ConfigError("El campo 'query' no puede estar vacío.")
        max_res = int(entry.get("max_results", 20))
        queries.append(SearchQuery(store=store, query=q, max_results=max_res))

    return queries


def load_settings(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Carga Settings desde un archivo YAML opcional y variables de entorno."""
    environ = os.environ if env is None else env
    data: dict = {}
    if path is not None:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ConfigError("El archivo de configuración debe contener un objeto YAML raíz.")
            data = loaded

    postal_codes = (
        _validate_postal_codes(data["postal_codes"])
        if "postal_codes" in data
        else DEFAULT_POSTAL_CODES
    )
    rules = _parse_rules(data.get("rules"))
    targets = _parse_targets(data.get("targets"))
    queries = _parse_queries(data.get("queries"))

    mon = data.get("monitor") or {}
    if not isinstance(mon, dict):
        raise ConfigError("La sección 'monitor' debe ser un mapeo.")

    day_interval = int(mon.get("day_interval_minutes", mon.get("interval_minutes", 10)))
    night_interval = int(mon.get("night_interval_minutes", 20))
    return Settings(
        postal_codes=postal_codes,
        rules=rules,
        interval_minutes=day_interval,
        day_interval_minutes=day_interval,
        night_interval_minutes=night_interval,
        day_start_hour=int(mon.get("day_start_hour", 6)),
        day_end_hour=int(mon.get("day_end_hour", 23)),
        min_delay_seconds=float(mon.get("min_delay_seconds", 4.0)),
        block_backoff_minutes=int(mon.get("block_backoff_minutes", 60)),
        block_notice_cooldown_hours=int(mon.get("block_notice_cooldown_hours", 24)),
        queries_per_run=int(mon.get("queries_per_run", 0)),
        db_path=str(mon.get("db_path", "ofertas.db")),
        headless=bool(mon.get("headless", True)),
        targets=targets,
        queries=queries,
        flights=_parse_flights(data.get("flights"), environ),
        telegram_token=environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=environ.get("TELEGRAM_CHAT_ID") or None,
        ml_access_token=environ.get("ML_ACCESS_TOKEN") or None,
    )
