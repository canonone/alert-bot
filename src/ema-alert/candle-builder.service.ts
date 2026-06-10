import { Injectable } from '@nestjs/common'

export interface Candle {
  open: number
  high: number
  low: number
  close: number
  timestamp: number
  complete: boolean
}

const TIMEFRAME_MS: Record<string, number> = {
  '5M': 5 * 60 * 1000,
  '30M': 30 * 60 * 1000,
}

const MAX_CANDLES = 100

@Injectable()
export class CandleBuilderService {
  private readonly openCandles = new Map<string, Candle>()
  private readonly completedCandles = new Map<string, Candle[]>()

  processTick(symbol: string, timeframe: string, price: number, timestamp: number): Candle | null {
    const windowMs = TIMEFRAME_MS[timeframe]
    if (!windowMs) return null

    const key = `${symbol}_${timeframe}`
    const candleOpenTime = Math.floor(timestamp / windowMs) * windowMs
    const current = this.openCandles.get(key)

    if (!current) {
      this.openCandles.set(key, {
        open: price, high: price, low: price, close: price,
        timestamp: candleOpenTime, complete: false,
      })
      return null
    }

    // Ignore ticks older than current candle
    if (timestamp < current.timestamp) return null

    if (timestamp >= current.timestamp + windowMs) {
      // New candle window — complete the previous
      const completed: Candle = { ...current, complete: true }
      const store = this.completedCandles.get(key) ?? []
      store.push(completed)
      if (store.length > MAX_CANDLES) store.shift()
      this.completedCandles.set(key, store)

      this.openCandles.set(key, {
        open: price, high: price, low: price, close: price,
        timestamp: candleOpenTime, complete: false,
      })
      return completed
    }

    // Same window — update open candle
    current.high = Math.max(current.high, price)
    current.low = Math.min(current.low, price)
    current.close = price
    return null
  }

  getCandles(symbol: string, timeframe: string): Candle[] {
    return this.completedCandles.get(`${symbol}_${timeframe}`) ?? []
  }

  seedCandles(symbol: string, timeframe: string, candles: Candle[]): void {
    const key = `${symbol}_${timeframe}`
    this.completedCandles.set(key, candles.slice(-MAX_CANDLES))
  }
}
