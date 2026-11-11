"""Orquestador principal: coordina scrapers, detección, SQLite y notificaciones."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Mapping, Protocol, Sequence

from playwright.async_api import BrowserContext

from ofertas_monitor.config import Target
from ofertas_monitor.detection import Deal, DealRules, evaluate
from ofertas_monitor.models import ProductSnapshot, Store
from ofertas_monitor.repository import Repository
from ofertas_monitor.stores.base import BlockedError, StoreScraper

logger = logging.getLogger(__name__)


class NotifierProtocol(Protocol):
    async def send_deal(self, deal: Deal) -> None: ...
    async def send_text(self, text: str) -> None: ...


@dataclass
class RunReport:
    snapshots: list[ProductSnapshot] = field(default_factory=list)
    deals: list[Deal] = field(default_factory=list)
    notified: list[Deal] = field(default_factory=list)
    blocked_stores: set[Store] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)


class Monitor:
    def __init__(
        self,
        *,
        scrapers: Mapping[Store, StoreScraper],
        repository: Repository,
        notifier: NotifierProtocol | None,
        postal_codes: Sequence[str],
        rules: DealRules = DealRules(),
        min_delay_seconds: float = 4.0,
        block_backoff: timedelta = timedelta(minutes=60),
        block_notice_cooldown: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.scrapers = dict(scrapers)
        self.repository = repository
        self.notifier = notifier
        self.postal_codes = tuple(postal_codes)
        self.rules = rules
        self.min_delay_seconds = min_delay_seconds
        self.block_backoff = block_backoff
        self.block_notice_cooldown = block_notice_cooldown
        self._clock = clock
        self._sleep = sleep
        self._blocked_until: dict[Store, datetime] = {}
        self._seen_in_cycle: set[Store] = set()

    def is_store_blocked(self, store: Store, *, now: datetime | None = None) -> bool:
        """Revisa el bloqueo en memoria y el persistido en SQLite (sobrevive entre corridas de GitHub Actions)."""
        ref = now or self._clock()
        until = self._blocked_until.get(store)
        if until is None:
            until = self.repository.get_meta_datetime(f"blocked_until:{store.value}")
            if until is not None:
                self._blocked_until[store] = until
        return until is not None and ref < until

    async def _register_block(self, store: Store, exc: Exception, now: datetime) -> None:
        until = now + self.block_backoff
        self._blocked_until[store] = until
        self.repository.set_meta_datetime(f"blocked_until:{store.value}", until)

        msg = (
            f"⚠️ {store.label} presentó CAPTCHA o bloqueo ({exc}). "
            f"Tienda en pausa por {int(self.block_backoff.total_seconds() // 60)} min "
            f"(sin intento de evasión)."
        )
        logger.warning(msg)

        notice_key = f"block_notice:{store.value}"
        last_notice = self.repository.get_meta_datetime(notice_key)
        if last_notice is not None and now - last_notice < self.block_notice_cooldown:
            return  # ya se avisó hace poco: no saturar el Telegram
        self.repository.set_meta_datetime(notice_key, now)
        await self._safe_send_text(msg)

    async def _safe_send_text(self, text: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.send_text(text)
        except Exception as exc:  # un fallo de Telegram no debe tumbar la corrida
            logger.error("No se pudo enviar aviso a Telegram: %s", exc)

    async def run_once(
        self,
        context: BrowserContext,
        targets: Sequence[Target],
    ) -> RunReport:
        report = RunReport()
        self._seen_in_cycle.clear()

        for target in targets:
            now = self._clock()
            store = target.store

            if self.is_store_blocked(store, now=now):
                report.blocked_stores.add(store)
                logger.warning("Tienda %s en pausa por bloqueo previo; se omite %s", store.label, target.url)
                continue

            scraper = self.scrapers.get(store)
            if scraper is None:
                report.errors[target.url] = f"Sin scraper configurado para {store.value}"
                continue

            if store in self._seen_in_cycle and self.min_delay_seconds > 0:
                jitter = random.uniform(0.0, 0.35 * self.min_delay_seconds)
                await self._sleep(self.min_delay_seconds + jitter)
            self._seen_in_cycle.add(store)

            try:
                snapshot = await scraper.fetch(context, target.url, self.postal_codes)
            except BlockedError as exc:
                report.blocked_stores.add(store)
                await self._register_block(store, exc, now)
                continue
            except Exception as exc:
                logger.error("Error extrayendo %s: %s", target.url, exc)
                report.errors[target.url] = str(exc)
                continue

            snapshot.captured_at = now
            report.snapshots.append(snapshot)

            history_median = self.repository.price_median(
                snapshot.store,
                snapshot.sku,
                min_samples=self.rules.min_history_samples,
                now=now,
            )
            deal = evaluate(snapshot, self.rules, history_median=history_median)
            self.repository.record_price(snapshot)

            if deal is not None:
                report.deals.append(deal)
                if self.repository.should_notify(deal, now=now):
                    if self.notifier is not None:
                        try:
                            await self.notifier.send_deal(deal)
                        except Exception as exc:
                            logger.error("No se pudo enviar la oferta a Telegram: %s", exc)
                            report.errors[snapshot.url] = f"Telegram: {exc}"
                            continue  # no se marca como notificada: se reintenta en la próxima corrida
                    self.repository.mark_notified(deal, now=now)
                    report.notified.append(deal)

        return report

