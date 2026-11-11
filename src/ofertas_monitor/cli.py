"""Interfaz de línea de comandos para ejecutar el monitor de ofertas."""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from ofertas_monitor.config import Settings, current_interval_minutes, load_settings, select_query_batch
from ofertas_monitor.discovery import StoreDiscoverer
from ofertas_monitor.flights import FlightMonitor
from ofertas_monitor.models import Store
from ofertas_monitor.monitor import Monitor
from ofertas_monitor.notifier.telegram import TelegramNotifier
from ofertas_monitor.repository import Repository
from ofertas_monitor.stores.amazon import AmazonScraper
from ofertas_monitor.stores.base import StoreScraper
from ofertas_monitor.stores.mercadolibre import MercadoLibreScraper
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
)
from ofertas_monitor.stores.walmart import WalmartScraper


def build_default_scrapers(settings: Settings) -> dict[Store, StoreScraper]:
    return {
        Store.WALMART: WalmartScraper(),
        Store.CHEDRAUI: ChedrauiScraper(),
        Store.ELEKTRA: ElektraScraper(),
        Store.AMAZON: AmazonScraper(),
        Store.MERCADOLIBRE: MercadoLibreScraper(access_token=settings.ml_access_token),
        Store.OFFICE_DEPOT: OfficeDepotScraper(),
        Store.LA_COMER: LaComerScraper(),
        Store.BODEGAS_ALIANZA: BodegasAlianzaScraper(),
        Store.SUBURBIA: SuburbiaScraper(),
        Store.COPPEL: CoppelScraper(),
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ofertas-monitor",
        description="Monitorea ofertas extremas, errores de precio y vuelos baratos en México.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Ruta al archivo YAML de configuración (default: config.yaml).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Ejecuta una sola pasada y termina.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No envía mensajes a Telegram; solo registra en consola y SQLite.",
    )
    parser.add_argument(
        "--solo-vuelos",
        action="store_true",
        help="Solo corre el cazador de vuelos (forzado, sin esperar su intervalo).",
    )
    return parser.parse_args(argv)


async def _run_flights(
    settings: Settings,
    repo: Repository,
    notifier: TelegramNotifier | None,
    *,
    context=None,
    force: bool,
) -> None:
    fs = settings.flights
    if not fs.enabled:
        return
    if not fs.active:
        logging.info("Vuelos desactivados: configura flights.portals o TRAVELPAYOUTS_TOKEN.")
        return
    try:
        report = await FlightMonitor(settings=fs, repository=repo, notifier=notifier).run_once(
            context=context, force=force
        )
    except Exception as exc:  # los vuelos nunca deben tumbar el monitor de productos
        logging.error("Error en el cazador de vuelos: %s", exc)
        return
    if not report.skipped:
        logging.info(
            "Vuelos: %d tarifas revisadas, %d ofertas, %d notificadas, %d errores.",
            len(report.offers), len(report.deals), len(report.notified), len(report.errors),
        )


async def _run(args: argparse.Namespace) -> int:
    cfg_path = args.config.resolve() if args.config.exists() else None
    if cfg_path is not None:
        load_dotenv(cfg_path.parent / ".env")
    load_dotenv()
    settings = load_settings(cfg_path)

    db_path = Path(settings.db_path)
    if not db_path.is_absolute() and cfg_path is not None:
        db_path = cfg_path.parent / db_path

    notifier: TelegramNotifier | None = None
    if not args.dry_run and settings.telegram_token and settings.telegram_chat_id:
        notifier = TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)
    else:
        logging.info("Modo sin notificaciones de Telegram (dry-run o credenciales no definidas).")

    with Repository(db_path) as repo:
        monitor = Monitor(
            scrapers=build_default_scrapers(settings),
            repository=repo,
            notifier=notifier,
            postal_codes=settings.postal_codes,
            rules=settings.rules,
            min_delay_seconds=settings.min_delay_seconds,
            block_backoff=timedelta(minutes=settings.block_backoff_minutes),
            block_notice_cooldown=timedelta(hours=settings.block_notice_cooldown_hours),
        )

        async with async_playwright() as pw:
            browser = None
            for extra in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
                try:
                    browser = await pw.chromium.launch(headless=settings.headless, **extra)
                    break
                except Exception:
                    continue
            if browser is None:
                browser = await pw.chromium.launch(headless=settings.headless)
            try:
                if args.solo_vuelos:
                    context = await browser.new_context(
                        locale="es-MX",
                        timezone_id="America/Mexico_City",
                    )
                    try:
                        await _run_flights(settings, repo, notifier, context=context, force=True)
                    finally:
                        await context.close()
                    return 0

                while True:
                    # 1) Productos: lote rotativo de búsquedas para que cada corrida sea corta
                    batch_idx = int(repo.get_meta("query_batch") or 0)
                    batch, next_idx = select_query_batch(
                        settings.queries, settings.queries_per_run, batch_idx
                    )
                    repo.set_meta("query_batch", str(next_idx))
                    blocked = {s for s in Store if monitor.is_store_blocked(s)}
                    batch = [q for q in batch if q.store not in blocked]

                    context = await browser.new_context(
                        locale="es-MX",
                        timezone_id="America/Mexico_City",
                    )
                    try:
                        # 2) Vuelos (portales web como Viajes Suburbia, Despegar, Volaris, etc. + API opcional)
                        await _run_flights(settings, repo, notifier, context=context, force=False)

                        discoverer = StoreDiscoverer(rules=settings.rules)
                        discovered = await discoverer.discover(context, batch)
                        all_targets = list(settings.targets) + discovered
                        logging.info(
                            "Iniciando corrida: %d búsquedas (lote %d), %d productos fijos + %d descubiertos.",
                            len(batch),
                            batch_idx,
                            len(settings.targets),
                            len(discovered),
                        )
                        report = await monitor.run_once(context, all_targets)
                        logging.info(
                            "Corrida completada: %d productos leídos, %d ofertas, %d notificadas, %d bloqueos, %d errores.",
                            len(report.snapshots),
                            len(report.deals),
                            len(report.notified),
                            len(report.blocked_stores),
                            len(report.errors),
                        )
                    finally:
                        await context.close()

                    if args.once:
                        break
                    wait_mins = current_interval_minutes(settings)
                    logging.info(
                        "Próxima corrida en %d minutos (horario 06:00-23:00 = %d min, 23:00-06:00 = %d min).",
                        wait_mins,
                        settings.day_interval_minutes,
                        settings.night_interval_minutes,
                    )
                    await asyncio.sleep(wait_mins * 60)
            finally:
                await browser.close()

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    return asyncio.run(_run(args))

