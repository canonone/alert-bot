import { Injectable, Logger } from '@nestjs/common'
import axios from 'axios'

@Injectable()
export class MarketDataService {
  private readonly logger = new Logger(MarketDataService.name)
  private readonly baseUrl = 'https://api.twelvedata.com'

  // ── Symbol formatter ──────────────────────────────────────────

  private formatSymbol(symbol: string): string {
    if (symbol === 'XAUUSD') return 'XAU/USD'
    if (symbol === 'XAGUSD') return 'XAG/USD'
    if (symbol.length === 6) return `${symbol.slice(0, 3)}/${symbol.slice(3)}`
    return symbol
  }

  // ── Get current price using a specific user's API key ────────

  async getCurrentPrice(symbol: string, apiKey: string): Promise<number | null> {
    try {
      const response = await axios.get(`${this.baseUrl}/price`, {
        params: {
          symbol: this.formatSymbol(symbol),
          apikey: apiKey,
        },
        timeout: 8000,
      })

      if (response.data?.status === 'error') {
        this.logger.error(`Twelve Data error for ${symbol}: ${response.data.message}`)
        return null
      }

      const price = parseFloat(response.data.price)
      return isNaN(price) ? null : price
    } catch (error) {
      this.logger.error(`Failed to fetch price for ${symbol}: ${error.message}`)
      return null
    }
  }

  // ── Validate an API key by making a test call ─────────────────
  // Returns true if key is valid, false otherwise

  async validateApiKey(apiKey: string): Promise<boolean> {
    try {
      const response = await axios.get(`${this.baseUrl}/price`, {
        params: {
          symbol: 'EUR/USD',
          apikey: apiKey,
        },
        timeout: 8000,
      })

      // Twelve Data returns status: 'error' with code 401/403 for bad keys
      if (response.data?.status === 'error') {
        return false
      }

      // Valid response has a numeric price field
      const price = parseFloat(response.data?.price)
      return !isNaN(price)
    } catch {
      return false
    }
  }

  // ── Get multiple prices in one batch call ─────────────────────
  // Twelve Data supports comma-separated symbols to save API calls

  async getBatchPrices(
    symbols: string[],
    apiKey: string,
  ): Promise<Record<string, number>> {
    if (symbols.length === 0) return {}

    const formatted = symbols.map((s) => this.formatSymbol(s)).join(',')

    try {
      const response = await axios.get(`${this.baseUrl}/price`, {
        params: {
          symbol: formatted,
          apikey: apiKey,
        },
        timeout: 10000,
      })

      const result: Record<string, number> = {}

      if (symbols.length === 1) {
        // Single symbol returns { price: "1.2345" } directly
        const price = parseFloat(response.data?.price)
        if (!isNaN(price)) result[symbols[0]] = price
      } else {
        // Multiple symbols return { "EUR/USD": { price: "1.2345" }, ... }
        for (const symbol of symbols) {
          const key = this.formatSymbol(symbol)
          const price = parseFloat(response.data?.[key]?.price)
          if (!isNaN(price)) result[symbol] = price
        }
      }

      return result
    } catch (error) {
      this.logger.error(`Batch price fetch failed: ${error.message}`)
      return {}
    }
  }
}
