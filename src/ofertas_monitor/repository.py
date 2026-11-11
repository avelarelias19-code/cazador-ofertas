"""Persistencia SQLite para historial de precios (mediana) y deduplicación de alertas."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median

from ofertas_monitor.detection import Deal
from ofertas_monitor.models import ProductSnapshot, Store


class Repository:
    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        alert_cooldown: timedelta = timedelta(hours=6),
        min_extra_drop_pct: float = 5.0,
    ) -> None:
        self.db_path = str(db_path)
        self.alert_cooldown = alert_cooldown
        self.min_extra_drop_pct = min_extra_drop_pct
        self._conn = sqlite3.connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store TEXT NOT NULL,
                sku TEXT NOT NULL,
                price TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_history_store_sku
                ON price_history(store, sku, captured_at);

            CREATE TABLE IF NOT EXISTS sent_alerts (
                store TEXT NOT NULL,
                sku TEXT NOT NULL,
                last_price TEXT NOT NULL,
                notified_at TEXT NOT NULL,
                PRIMARY KEY (store, sku)
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta_datetime(self, key: str) -> datetime | None:
        raw = self.get_meta(key)
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def set_meta_datetime(self, key: str, value: datetime) -> None:
        self.set_meta(key, value.astimezone(timezone.utc).isoformat())

    # ----------------------------------------------- historial genérico (vuelos)
    def record_value(self, namespace: str, key: str, price: Decimal, captured_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO price_history (store, sku, price, captured_at) VALUES (?, ?, ?, ?)",
            (namespace, key, str(price), captured_at.astimezone(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def median_value(
        self,
        namespace: str,
        key: str,
        *,
        days: int = 30,
        min_samples: int = 3,
        now: datetime | None = None,
    ) -> Decimal | None:
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (ref - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT price FROM price_history WHERE store = ? AND sku = ? AND captured_at >= ?",
            (namespace, key, cutoff),
        ).fetchall()
        if len(rows) < min_samples:
            return None
        return Decimal(str(median(Decimal(r[0]) for r in rows)))

    def should_notify_key(
        self, namespace: str, key: str, price: Decimal, *, now: datetime | None = None
    ) -> bool:
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        row = self._conn.execute(
            "SELECT last_price, notified_at FROM sent_alerts WHERE store = ? AND sku = ?",
            (namespace, key),
        ).fetchone()
        if row is None:
            return True
        last_price = Decimal(row[0])
        notified_at = datetime.fromisoformat(row[1])
        if notified_at.tzinfo is None:
            notified_at = notified_at.replace(tzinfo=timezone.utc)
        if ref - notified_at >= self.alert_cooldown:
            return True
        if last_price > 0:
            extra_drop_pct = float((last_price - price) / last_price * Decimal("100"))
            if extra_drop_pct >= self.min_extra_drop_pct:
                return True
        return False

    def mark_notified_key(
        self, namespace: str, key: str, price: Decimal, *, now: datetime | None = None
    ) -> None:
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO sent_alerts (store, sku, last_price, notified_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(store, sku) DO UPDATE SET
                last_price = excluded.last_price,
                notified_at = excluded.notified_at
            """,
            (namespace, key, str(price), ref),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def record_price(self, snapshot: ProductSnapshot) -> None:
        ts = snapshot.captured_at.astimezone(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO price_history (store, sku, price, captured_at) VALUES (?, ?, ?, ?)",
            (snapshot.store.value, snapshot.sku, str(snapshot.price), ts),
        )
        self._conn.commit()

    def price_median(
        self,
        store: Store,
        sku: str,
        *,
        days: int = 30,
        min_samples: int = 3,
        now: datetime | None = None,
    ) -> Decimal | None:
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (ref - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """
            SELECT price FROM price_history
            WHERE store = ? AND sku = ? AND captured_at >= ?
            ORDER BY captured_at DESC
            """,
            (store.value, sku, cutoff),
        ).fetchall()
        if len(rows) < min_samples:
            return None
        values = [Decimal(r[0]) for r in rows]
        return Decimal(str(median(values)))

    def should_notify(self, deal: Deal, *, now: datetime | None = None) -> bool:
        return self.should_notify_key(
            deal.snapshot.store.value, deal.snapshot.sku, deal.snapshot.price, now=now
        )

    def mark_notified(self, deal: Deal, *, now: datetime | None = None) -> None:
        self.mark_notified_key(
            deal.snapshot.store.value, deal.snapshot.sku, deal.snapshot.price, now=now
        )

