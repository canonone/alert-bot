import logging
from dataclasses import dataclass

from app.services.market_data_service import MarketDataService

logger = logging.getLogger("LotSizeService")

# Which USD pair to fetch for each quote currency
CONVERSION_PAIR: dict[str, str] = {
    "JPY": "USDJPY",
    "CHF": "USDCHF",
    "CAD": "USDCAD",
    "AUD": "AUDUSD",
    "NZD": "NZDUSD",
    "GBP": "GBPUSD",
    "EUR": "EURUSD",
}

# Supported pairs and their quote currencies
PAIR_QUOTE: dict[str, str] = {
    # USD quoted — pip value always $10
    "EURUSD": "USD", "GBPUSD": "USD", "AUDUSD": "USD",
    "NZDUSD": "USD", "XAUUSD": "USD", "XAGUSD": "USD",
    # JPY quoted
    "USDJPY": "JPY", "EURJPY": "JPY", "GBPJPY": "JPY",
    "AUDJPY": "JPY", "NZDJPY": "JPY", "CADJPY": "JPY",
    "CHFJPY": "JPY",
    # CHF quoted
    "USDCHF": "CHF", "EURCHF": "CHF", "GBPCHF": "CHF",
    "AUDCHF": "CHF", "NZDCHF": "CHF", "CADCHF": "CHF",
    # CAD quoted
    "USDCAD": "CAD", "EURCAD": "CAD", "GBPCAD": "CAD",
    "AUDCAD": "CAD", "NZDCAD": "CAD",
    # AUD quoted
    "EURAUD": "AUD", "GBPAUD": "AUD", "NZDAUD": "AUD",
    # NZD quoted
    "EURNZD": "NZD", "GBPNZD": "NZD", "AUDNZD": "NZD",
    # GBP quoted
    "EURGBP": "GBP",
}


@dataclass
class LotSizeResult:
    success: bool
    pair: str
    risk_usd: float
    sl_pips: float
    pip_value: float
    conversion_pair: str | None
    conversion_rate: float | None
    lot_size: float
    mini_lots: float
    micro_lots: float
    message: str


class LotSizeService:
    def __init__(self, market_data: MarketDataService) -> None:
        self.market_data = market_data

    async def calculate(self, pair: str, risk_usd: float, sl_pips: float, api_key: str) -> LotSizeResult:
        upper_pair = pair.upper()

        # ── Validate pair ────────────────────────────────────────────
        quote_currency = PAIR_QUOTE.get(upper_pair)
        if not quote_currency:
            return self._error_result(
                pair, risk_usd, sl_pips,
                f"❌ <b>{upper_pair}</b> is not a supported pair.\n\nUse /pairs to see all supported pairs.",
            )

        # ── Validate inputs ──────────────────────────────────────────
        if risk_usd <= 0:
            return self._error_result(pair, risk_usd, sl_pips, "❌ Risk amount must be greater than 0.")
        if sl_pips <= 0:
            return self._error_result(pair, risk_usd, sl_pips, "❌ Stop loss pips must be greater than 0.")

        # ── Calculate pip value ──────────────────────────────────────
        conversion_pair: str | None = None
        conversion_rate: float | None = None

        if quote_currency == "USD":
            # Simple: pip value is always $10 per standard lot
            pip_value = 10.0
        else:
            # Need to fetch the USD conversion rate
            conversion_pair = CONVERSION_PAIR.get(quote_currency)

            if not conversion_pair:
                return self._error_result(
                    pair, risk_usd, sl_pips, f"❌ Could not determine conversion pair for <b>{upper_pair}</b>."
                )

            logger.info(f"Fetching {conversion_pair} for pip value conversion...")
            rate, _ = await self.market_data.get_current_price(conversion_pair, api_key)

            if rate is None:
                return self._error_result(
                    pair, risk_usd, sl_pips,
                    f"❌ Could not fetch live rate for <b>{conversion_pair}</b>.\n\n"
                    f"Please check your Twelve Data API key or try again shortly.",
                )

            conversion_rate = rate

            # Quote stronger than USD (CHF, JPY, CAD) → divide
            # Quote weaker than USD (AUD, NZD, GBP, EUR) → multiply
            division_currencies = ("JPY", "CHF", "CAD")

            if quote_currency == "JPY":
                # JPY pip size is 0.01 so we use 1000 instead of 10
                pip_value = 1000 / rate
            elif quote_currency in division_currencies:
                pip_value = 10 / rate
            else:
                # AUD, NZD, GBP, EUR — multiply
                pip_value = 10 * rate

        # ── Final lot size calculation ────────────────────────────────
        # Lot size = Risk$ ÷ (SL pips × Pip value per std lot)
        lot_size = risk_usd / (sl_pips * pip_value)
        rounded = round(lot_size * 100) / 100  # 2 decimal places
        mini_lots = round(lot_size * 10 * 10) / 10  # 1 decimal
        micro_lots = round(lot_size * 100 * 10) / 10  # 1 decimal

        # ── Build response message ────────────────────────────────────
        conversion_line = (
            f"\n📡 <b>{conversion_pair} Rate:</b> {conversion_rate:.5f}" if conversion_pair else ""
        )

        message = (
            f"💰 <b>Lot Size — {upper_pair}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Pair:</b> {upper_pair}\n"
            f"💵 <b>Risk:</b> ${risk_usd:.2f}\n"
            f"🛑 <b>SL:</b> {sl_pips} pip{'s' if sl_pips != 1 else ''}\n"
            f"🎯 <b>Pip Value:</b> ${pip_value:.4f}{conversion_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Lot Size:   {rounded} lots</b>\n"
            f"Mini lots:  {mini_lots}\n"
            f"Micro lots: {micro_lots}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Rate fetched live at time of calculation</i>"
        )

        return LotSizeResult(
            success=True,
            pair=upper_pair,
            risk_usd=risk_usd,
            sl_pips=sl_pips,
            pip_value=pip_value,
            conversion_pair=conversion_pair,
            conversion_rate=conversion_rate,
            lot_size=rounded,
            mini_lots=mini_lots,
            micro_lots=micro_lots,
            message=message,
        )

    # ── Supported pairs list message ──────────────────────────────

    def get_supported_pairs_message(self) -> str:
        usd = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"]
        jpy = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"]
        chf = ["USDCHF", "EURCHF", "GBPCHF", "AUDCHF", "NZDCHF", "CADCHF"]
        cad = ["USDCAD", "EURCAD", "GBPCAD", "AUDCAD", "NZDCAD"]
        others = ["EURAUD", "GBPAUD", "EURNZD", "GBPNZD", "AUDNZD", "EURGBP"]

        return (
            f"📋 <b>Supported Pairs</b>\n\n"
            f"<b>USD Quoted</b> (pip = $10 flat)\n"
            f"{' • '.join(usd)}\n\n"
            f"<b>JPY Quoted</b>\n"
            f"{' • '.join(jpy)}\n\n"
            f"<b>CHF Quoted</b>\n"
            f"{' • '.join(chf)}\n\n"
            f"<b>CAD Quoted</b>\n"
            f"{' • '.join(cad)}\n\n"
            f"<b>Other Crosses</b>\n"
            f"{' • '.join(others)}"
        )

    def _error_result(self, pair: str, risk_usd: float, sl_pips: float, message: str) -> LotSizeResult:
        return LotSizeResult(
            success=False,
            pair=pair,
            risk_usd=risk_usd,
            sl_pips=sl_pips,
            pip_value=0,
            conversion_pair=None,
            conversion_rate=None,
            lot_size=0,
            mini_lots=0,
            micro_lots=0,
            message=message,
        )
