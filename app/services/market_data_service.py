import logging

import httpx

logger = logging.getLogger("MarketDataService")

BASE_URL = "https://api.twelvedata.com"


class MarketDataService:
    # ── Symbol formatter ──────────────────────────────────────────

    def _format_symbol(self, symbol: str) -> str:
        if symbol == "XAUUSD":
            return "XAU/USD"
        if symbol == "XAGUSD":
            return "XAG/USD"
        if len(symbol) == 6:
            return f"{symbol[:3]}/{symbol[3:]}"
        return symbol

    # ── Quota error detection ─────────────────────────────────────

    def is_quota_error(self, data: dict) -> bool:
        if not isinstance(data, dict):
            return False
        message = data.get("message") or ""
        return data.get("status") == "error" and (
            data.get("code") == 429 or "out of API credits" in message or "API credits" in message
        )

    # ── Get current price using a specific user's API key ────────

    async def get_current_price(self, symbol: str, api_key: str) -> tuple[float | None, bool]:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{BASE_URL}/price",
                    params={"symbol": self._format_symbol(symbol), "apikey": api_key},
                )
            data = response.json()

            if isinstance(data, dict) and data.get("status") == "error":
                if self.is_quota_error(data):
                    return None, True
                logger.error(f"Twelve Data error for {symbol}: {data.get('message')}")
                return None, False

            try:
                price = float(data.get("price"))
            except (TypeError, ValueError):
                return None, False
            return price, False
        except Exception as error:
            logger.error(f"Failed to fetch price for {symbol}: {error}")
            return None, False

    # ── Validate an API key by making a test call ─────────────────
    # Returns True if key is valid, False otherwise

    async def validate_api_key(self, api_key: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{BASE_URL}/price",
                    params={"symbol": "EUR/USD", "apikey": api_key},
                )
            data = response.json()

            if isinstance(data, dict) and data.get("status") == "error":
                return False

            try:
                float(data.get("price"))
            except (TypeError, ValueError):
                return False
            return True
        except Exception:
            return False

    # ── Get multiple prices in one batch call ─────────────────────
    # Twelve Data supports comma-separated symbols to save API calls

    async def get_batch_prices(self, symbols: list[str], api_key: str) -> tuple[dict[str, float], bool]:
        if not symbols:
            return {}, False

        formatted = ",".join(self._format_symbol(s) for s in symbols)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{BASE_URL}/price",
                    params={"symbol": formatted, "apikey": api_key},
                )
            data = response.json()

            if self.is_quota_error(data if isinstance(data, dict) else {}):
                return {}, True

            prices: dict[str, float] = {}

            if len(symbols) == 1:
                # Single symbol returns { price: "1.2345" } directly
                try:
                    prices[symbols[0]] = float(data.get("price"))
                except (TypeError, ValueError):
                    pass
            else:
                # Multiple symbols return { "EUR/USD": { price: "1.2345" }, ... }
                for symbol in symbols:
                    key = self._format_symbol(symbol)
                    try:
                        prices[symbol] = float(data.get(key, {}).get("price"))
                    except (TypeError, ValueError, AttributeError):
                        pass

            return prices, False
        except Exception as error:
            logger.error(f"Batch price fetch failed: {error}")
            return {}, False
