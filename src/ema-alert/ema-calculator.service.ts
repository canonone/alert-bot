import { Injectable } from '@nestjs/common'
import { Candle } from './candle-builder.service'

@Injectable()
export class EmaCalculatorService {
  calculate(candles: Candle[], period: number): number[] {
    if (candles.length < period) return []

    const multiplier = 2 / (period + 1)
    const emaValues: number[] = new Array(candles.length).fill(0)

    let sma = 0
    for (let i = 0; i < period; i++) sma += candles[i].close
    emaValues[period - 1] = sma / period

    for (let i = period; i < candles.length; i++) {
      emaValues[i] = (candles[i].close - emaValues[i - 1]) * multiplier + emaValues[i - 1]
    }

    return emaValues
  }

  detectCross(
    candles: Candle[],
    emaValues: number[],
  ): {
    crossed: boolean
    direction: 'cross_up' | 'cross_down' | null
    currentPrice: number
    emaValue: number
  } {
    const noCross = { crossed: false, direction: null as null, currentPrice: 0, emaValue: 0 }
    const n = candles.length
    if (n < 2 || emaValues.length < n) return noCross

    const prevClose = candles[n - 2].close
    const currClose = candles[n - 1].close
    const prevEma = emaValues[n - 2]
    const currEma = emaValues[n - 1]

    if (prevEma === 0 || currEma === 0) return noCross

    if (prevClose < prevEma && currClose > currEma) {
      return { crossed: true, direction: 'cross_up', currentPrice: currClose, emaValue: currEma }
    }
    if (prevClose > prevEma && currClose < currEma) {
      return { crossed: true, direction: 'cross_down', currentPrice: currClose, emaValue: currEma }
    }

    return noCross
  }
}
