import asyncio
import datetime
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

from app import config
from app.services.price_alert_service import PriceAlertService

logger = logging.getLogger("FinnhubPriceService")

SendMessage = Callable[[str, str], Awaitable[None]]


class FinnhubPriceService:
    def __init__(
        self,
        price_alert_service: PriceAlertService,
        send_message: SendMessage,
        daily_sweep_service: Any = None,
    ) -> None:
        self.price_alert_service = price_alert_service
        self.send_message = send_message
        self.finnhub_api_key = config.FINNHUB_API_KEY

        # Optional Daily Sweep feature — ticks are forwarded to it when configured, and every pair
        # with an ACTIVE setup is subscribed even if nobody has a normal price alert on it
        self.daily_sweep_service = daily_sweep_service

        self.ws: Any | None = None
        self.subscribed_symbols: set[str] = set()
        self.last_price: dict[str, float] = {}
        self.last_price_at: dict[str, datetime.datetime] = {}  # UTC time of each symbol's latest tick
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10

        self._run_task: asyncio.Task | None = None
        self._closing = False

    def start(self) -> None:
        self._run_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closing = True
        if self._run_task:
            self._run_task.cancel()
        if self.ws is not None:
            await self.ws.close()
        logger.info("[FinnhubPrice] WebSocket closed")

    async def _run(self) -> None:
        url = f"wss://ws.finnhub.io?token={self.finnhub_api_key}"

        while not self._closing:
            try:
                async with websockets.connect(url) as ws:
                    self.ws = ws
                    logger.info("[FinnhubPrice] WebSocket connected")
                    self.reconnect_attempts = 0
                    self.subscribed_symbols.clear()
                    asyncio.create_task(self._subscribe_to_active_symbols())

                    async for raw_message in ws:
                        await self._handle_message(raw_message)

                logger.warning("[FinnhubPrice] WebSocket disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(f"[FinnhubPrice] WebSocket error: {error}")

            self.ws = None
            if self._closing:
                break
            if not await self._wait_for_reconnect():
                break

    async def _wait_for_reconnect(self) -> bool:
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error("[FinnhubPrice] Max reconnect attempts reached, giving up")
            return False
        delay = 5 * (self.reconnect_attempts + 1)
        self.reconnect_attempts += 1
        logger.info(f"[FinnhubPrice] Reconnecting in {delay}s (attempt {self.reconnect_attempts})")
        await asyncio.sleep(delay)
        return True

    async def _subscribe_to_active_symbols(self) -> None:
        grouped = await self.price_alert_service.get_active_alerts_grouped_by_symbol()
        sweep_symbols = self.daily_sweep_service.active_symbols() if self.daily_sweep_service is not None else set()
        symbols = list(set(grouped.keys()) | sweep_symbols)

        if not symbols:
            logger.info("[FinnhubPrice] No active symbols to subscribe")
            return

        async def subscribe_after_delay(index: int, symbol: str) -> None:
            await asyncio.sleep(index * 0.2)
            ws = self.ws
            if ws is not None:
                finnhub_symbol = self._to_finnhub_symbol(symbol)
                await ws.send(json.dumps({"type": "subscribe", "symbol": finnhub_symbol}))
                self.subscribed_symbols.add(symbol)
                logger.info(f"[FinnhubPrice] Subscribed to: {finnhub_symbol}")

        for index, symbol in enumerate(symbols):
            asyncio.create_task(subscribe_after_delay(index, symbol))

    async def subscribe_to_symbol(self, symbol: str) -> None:
        if symbol in self.subscribed_symbols:
            return
        if self.ws is None:
            return
        finnhub_symbol = self._to_finnhub_symbol(symbol)
        await self.ws.send(json.dumps({"type": "subscribe", "symbol": finnhub_symbol}))
        self.subscribed_symbols.add(symbol)
        logger.info(f"[FinnhubPrice] Dynamically subscribed to: {finnhub_symbol}")

    async def _handle_message(self, data: str) -> None:
        try:
            parsed = json.loads(data)
        except ValueError:
            return

        if parsed.get("type") == "trade":
            for trade in parsed.get("data") or []:
                symbol = self._from_finnhub_symbol(trade["s"])
                price = trade["p"]
                previous_price = self.last_price.get(symbol)
                self.last_price[symbol] = price
                self.last_price_at[symbol] = datetime.datetime.now(datetime.timezone.utc)
                await self._check_price_alerts(symbol, price, previous_price)
                if self.daily_sweep_service is not None:
                    try:
                        await self.daily_sweep_service.check_tick(symbol, price, previous_price)
                    except Exception as error:
                        logger.error(f"[FinnhubPrice] Daily Sweep tick check failed for {symbol}: {error}")
        elif parsed.get("type") == "error":
            logger.error(f"[FinnhubPrice] API error: {parsed.get('msg')}")

    def get_last_tick(self, symbol: str) -> tuple[float, datetime.datetime] | None:
        """Latest (price, UTC receive time) for `symbol`, or None if no tick has arrived yet."""
        at = self.last_price_at.get(symbol)
        if at is None:
            return None
        return self.last_price[symbol], at

    async def _check_price_alerts(self, symbol: str, price: float, previous_price: float | None) -> None:
        triggered = await self.price_alert_service.check_tick_against_alerts(symbol, price, previous_price)
        for item in triggered:
            message = self.price_alert_service.build_alert_message(item.alert, item.current_price, item.outcome)
            await self.send_message(item.alert.chat_id, message)
            logger.info(
                f"[FinnhubPrice] Price alert #{item.alert.user_alert_id} {item.outcome} — "
                f"{symbol} {item.alert.type} @ {item.alert.target_price} for {item.alert.chat_id}"
            )

    def _to_finnhub_symbol(self, symbol: str) -> str:
        return f"OANDA:{symbol[:3]}_{symbol[3:]}"

    def _from_finnhub_symbol(self, finnhub_symbol: str) -> str:
        return finnhub_symbol.replace("OANDA:", "").replace("_", "")
