# `ofertas_monitor` — Monitor de Ofertas Extremas y Errores de Precio (México)

Monitor modular en Python para detectar **descuentos mayores al 65%** o **errores de precio** (caídas abruptas respecto a la mediana histórica de 30 días o precios simbólicos) en:

- **Walmart México** (`WalmartScraper` — Playwright `__NEXT_DATA__` + fallback JSON-LD)
- **Chedraui** (`ChedrauiScraper` — plataforma VTEX: catálogo + simulación logística por CP)
- **Elektra** (`ElektraScraper` — plataforma VTEX: catálogo + simulación logística por CP)
- **Bodegas Alianza** (`BodegasAlianzaScraper` — plataforma VTEX: catálogo + simulación logística por CP)
- **Suburbia** (`SuburbiaScraper` — Playwright JSON-LD + DOM + verificación por CP)
- **Coppel** (`CoppelScraper` — Playwright JSON-LD + DOM + verificación por CP)
- **Office Depot México** (`OfficeDepotScraper` — Playwright JSON-LD + DOM + verificación por CP)
- **La Comer** (`LaComerScraper` — Playwright JSON-LD + DOM + verificación por CP)
- **Amazon México** (`AmazonScraper` — Playwright DOM + modal de ubicación GLUX por CP)
- **Mercado Libre México** (`MercadoLibreScraper` — API + fallback Playwright sin token)

## Características principales

1. **Filtro estricto de vendedor oficial (`seller_filter.py`):** descarta cualquier oferta vendida por terceros o revendedores de marketplace (`sellerId == "1"` en VTEX, `AVDBXBAVVSXLQ` / retail en Amazon, `official_store_id` no nulo en Mercado Libre, Walmart 1P).
2. **Verificación automática en 5 códigos postales (`postal_codes`):** comprueba inventario o envío real en 5 CPs clave de **CDMX (incluyendo Iztapalapa), Puebla y Querétaro** antes de emitir cualquier alerta (`06600` CDMX Centro, `09000` Iztapalapa, `04500` CDMX Sur, `72000` Puebla y `76000` Querétaro).
3. **Notificación a Telegram con botón de compra (`notifier/telegram.py`):** envía alerta formateada en HTML con un botón inline `🛒 Comprar ahora` que abre directamente la URL del producto, más manejo de `retry_after` en HTTP 429 y deduplicación en SQLite.
4. **Respeto a controles anti-bot:** si una tienda muestra CAPTCHA o devuelve HTTP 403/429, lanza `BlockedError`, pone la tienda en pausa (`block_backoff_minutes`) sin intentar resolver ni evadir el reto y notifica al operador.

## Instalación

```powershell
cd C:\Users\avela\.gemini\antigravity\scratch\ofertas_monitor
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\pip install -e .
.\.venv\Scripts\playwright install chromium
```

## Ejecución de tests

```powershell
# Suite completa (unit + E2E en Playwright con sitios simulados offline)
.\.venv\Scripts\pytest -v

# Smoke tests opcionales contra sitios reales
.\.venv\Scripts\pytest --run-live -v
```

## Uso

1. Copia `.env.example` a `.env` y configura `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` y (opcionalmente) `ML_ACCESS_TOKEN`.
2. Copia `config.example.yaml` a `config.yaml` y agrega las URLs de productos a monitorear.
3. Ejecuta una corrida única o el bucle continuo:

```powershell
# Una sola pasada
.\.venv\Scripts\python -m ofertas_monitor --config config.yaml --once

# Monitoreo periódico continuo
.\.venv\Scripts\python -m ofertas_monitor --config config.yaml
```

