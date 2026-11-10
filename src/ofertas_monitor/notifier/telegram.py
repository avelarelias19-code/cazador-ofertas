"""Cliente asíncrono de Telegram Bot API con botón inline de compra y manejo de 429."""
from __future__ import annotations

import asyncio
import html
from typing import Any, Awaitable, Callable

import httpx

from ofertas_monitor.detection import Deal


class TelegramError(RuntimeError):
    """Error al enviar mensaje a Telegram (sin exponer el token del bot)."""


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._client = client
        self._sleep = sleep

    @property
    def _endpoint(self) -> str:
        return f"https://api.telegram.org/bot{self._token}/sendMessage"

    def build_payload(self, deal: Deal) -> dict[str, Any]:
        snap = deal.snapshot
        header = (
            "🚨 <b>POSIBLE ERROR DE PRECIO</b> 🚨"
            if deal.is_price_error
            else "🔥 <b>OFERTA EXTREMA DETECTADA</b> 🔥"
        )
        title = html.escape(snap.title)
        store_label = html.escape(snap.store.label)
        seller_name = html.escape(snap.seller.name)

        lines = [
            header,
            f"🏪 <b>Tienda:</b> {store_label} (Vendedor oficial: {seller_name})",
            f"📦 <b>Producto:</b> {title}",
            f"💰 <b>Precio oferta:</b> ${snap.price:,.2f} {html.escape(snap.currency)}",
        ]
        if snap.list_price is not None and snap.list_price > snap.price:
            lines.append(f"🏷️ <b>Precio lista:</b> ${snap.list_price:,.2f}")
        if deal.discount_pct is not None and deal.discount_pct > 0:
            lines.append(f"📉 <b>Descuento:</b> -{deal.discount_pct:.1f}%")
        if deal.history_median is not None:
            lines.append(f"📊 <b>Mediana histórica (30d):</b> ${deal.history_median:,.2f}")

        cps = ", ".join(deal.in_stock_postal_codes)
        lines.append(f"📍 <b>Stock confirmado en CPs:</b> {html.escape(cps)}")

        if deal.reasons:
            motivos = "; ".join(html.escape(r) for r in deal.reasons)
            lines.append(f"ℹ️ <b>Motivo:</b> {motivos}")

        safe_url = html.escape(snap.url, quote=True)
        lines.append(f'🔗 <b>Link directo:</b> <a href="{safe_url}">{safe_url}</a>')

        return {
            "chat_id": self._chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🛒 Comprar ahora", "url": snap.url}],
                ]
            },
        }

    async def send_deal(self, deal: Deal) -> None:
        await self._post(self.build_payload(deal))

    async def send_text(self, text: str) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": html.escape(text),
            "parse_mode": "HTML",
        }
        await self._post(payload)

    async def _post(self, payload: dict[str, Any], *, max_attempts: int = 3) -> None:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await client.post(self._endpoint, json=payload)
                except httpx.HTTPError as exc:
                    raise TelegramError("Fallo de red al contactar Telegram Bot API") from exc

                if resp.status_code == 200:
                    return

                data: dict[str, Any] = {}
                try:
                    data = resp.json()
                except ValueError:
                    pass

                if resp.status_code == 429 and attempt < max_attempts:
                    retry_after = float(data.get("parameters", {}).get("retry_after", 1))
                    await self._sleep(retry_after)
                    continue

                description = data.get("description") or f"HTTP {resp.status_code}"
                raise TelegramError(f"Telegram rechazó el mensaje: {description}")
        finally:
            if owns_client:
                await client.aclose()

