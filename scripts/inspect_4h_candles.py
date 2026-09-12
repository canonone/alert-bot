"""Manual verification tool for the 4H retracement-zone feature.

Prints Twelve Data's native `interval=4h` candles side-by-side with the synthetic 4H bars the
bot actually builds (aggregated from 1H candles — see retracement_zone_service.py for why: the
native endpoint closes bars 1 hour earlier than the user's real broker feed, FOREX.com).

Use this to confirm the synthetic bars close at 02/06/10/14/18/22 UTC (03/07/11/15/19/23 WAT),
matching FOREX.com/TradingView, while the native ones are visibly 1 hour off.

Usage:
    python -m scripts.inspect_4h_candles [PAIR]   # PAIR defaults to XAUUSD
"""

import asyncio
import datetime
import sys

from app import config
from app.services.market_data_service import MarketDataService
from app.services.retracement_zone_service import (
    ONE_H_BOUNDARY_SAMPLE_SIZE,
    _aggregate_1h_to_4h,
    _boundary_hours_for,
    _to_wat,
)


async def main() -> None:
    pair = sys.argv[1].upper() if len(sys.argv) > 1 else "XAUUSD"

    if not config.TWELVE_DATA_API_KEY:
        print("TWELVE_DATA_API_KEY is not set in .env — cannot inspect live data.")
        return

    market_data = MarketDataService()

    native, quota_exceeded = await market_data.get_candles(pair, "4h", config.TWELVE_DATA_API_KEY, outputsize=8)
    if quota_exceeded:
        print("Twelve Data quota exceeded — try again later.")
        return

    print(f"Twelve Data native interval=4h candles for {pair} (known to be 1h off FOREX.com):\n")
    if not native:
        print("  (none returned)")
    for c in native:
        print(f"  {c['datetime']:%Y-%m-%d %H:%M} UTC   O:{c['open']:<10} H:{c['high']:<10} L:{c['low']:<10} C:{c['close']:<10}")

    hourly, quota_exceeded = await market_data.get_candles(
        pair, "1h", config.TWELVE_DATA_API_KEY, outputsize=ONE_H_BOUNDARY_SAMPLE_SIZE
    )
    if quota_exceeded:
        print("\nTwelve Data quota exceeded on 1H fetch — try again later.")
        return
    if not hourly:
        print(f"\nNo 1H candle data returned for {pair}. Market may be closed, or the symbol is unsupported.")
        return

    boundary_hours = _boundary_hours_for(pair)
    synthetic = _aggregate_1h_to_4h(hourly, boundary_hours)

    print(f"\nSynthetic 4H bars aggregated from 1H data (this is what the bot uses), confirmed boundary {boundary_hours} UTC:\n")
    if not synthetic:
        print("  (no complete synthetic bar yet — not enough contiguous 1H history in this sample)")
    for c in synthetic:
        end = c["datetime"] + datetime.timedelta(hours=4)
        print(
            f"  {c['datetime']:%Y-%m-%d %H:%M} -> {end:%H:%M} UTC ({_to_wat(end):%H:%M} WAT close)   "
            f"O:{c['open']:<10} H:{c['high']:<10} L:{c['low']:<10} C:{c['close']:<10}"
        )

    print("\nCheck the synthetic bars' close times against your FOREX.com/TradingView chart —")
    print("they should land exactly on 02/06/10/14/18/22 UTC (03/07/11/15/19/23 WAT).")


if __name__ == "__main__":
    asyncio.run(main())
