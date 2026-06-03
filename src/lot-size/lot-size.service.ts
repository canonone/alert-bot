import { Injectable, Logger } from '@nestjs/common'
import { MarketDataService } from '../market-data/market-data.service'

export interface LotSizeResult {
  success: boolean
  pair: string
  riskUsd: number
  slPips: number
  pipValue: number
  conversionPair: string | null
  conversionRate: number | null
  lotSize: number
  miniLots: number
  microLots: number
  message: string
}

// Which USD pair to fetch for each quote currency
const CONVERSION_PAIR: Record<string, string> = {
  JPY: 'USDJPY',
  CHF: 'USDCHF',
  CAD: 'USDCAD',
  AUD: 'AUDUSD',
  NZD: 'NZDUSD',
  GBP: 'GBPUSD',
  EUR: 'EURUSD',
}

// Supported pairs and their quote currencies
const PAIR_QUOTE: Record<string, string> = {
  // USD quoted — pip value always $10
  EURUSD: 'USD', GBPUSD: 'USD', AUDUSD: 'USD',
  NZDUSD: 'USD', XAUUSD: 'USD', XAGUSD: 'USD',

  // JPY quoted
  USDJPY: 'JPY', EURJPY: 'JPY', GBPJPY: 'JPY',
  AUDJPY: 'JPY', NZDJPY: 'JPY', CADJPY: 'JPY',
  CHFJPY: 'JPY',

  // CHF quoted
  USDCHF: 'CHF', EURCHF: 'CHF', GBPCHF: 'CHF',
  AUDCHF: 'CHF', NZDCHF: 'CHF', CADCHF: 'CHF',

  // CAD quoted
  USDCAD: 'CAD', EURCAD: 'CAD', GBPCAD: 'CAD',
  AUDCAD: 'CAD', NZDCAD: 'CAD',

  // AUD quoted
  EURAUD: 'AUD', GBPAUD: 'AUD', NZDAUD: 'AUD',

  // NZD quoted
  EURNZD: 'NZD', GBPNZD: 'NZD', AUDNZD: 'NZD',

  // GBP quoted
  EURGBP: 'GBP',
}

@Injectable()
export class LotSizeService {
  private readonly logger = new Logger(LotSizeService.name)

  constructor(private readonly marketData: MarketDataService) {}

  async calculate(
    pair: string,
    riskUsd: number,
    slPips: number,
    apiKey: string,
  ): Promise<LotSizeResult> {
    const upperPair = pair.toUpperCase()

    // ── Validate pair ────────────────────────────────────────────
    const quoteCurrency = PAIR_QUOTE[upperPair]
    if (!quoteCurrency) {
      return this.errorResult(pair, riskUsd, slPips, `❌ <b>${upperPair}</b> is not a supported pair.\n\nUse /pairs to see all supported pairs.`)
    }

    // ── Validate inputs ──────────────────────────────────────────
    if (riskUsd <= 0) {
      return this.errorResult(pair, riskUsd, slPips, `❌ Risk amount must be greater than 0.`)
    }
    if (slPips <= 0) {
      return this.errorResult(pair, riskUsd, slPips, `❌ Stop loss pips must be greater than 0.`)
    }

    // ── Calculate pip value ──────────────────────────────────────
    let pipValue: number
    let conversionPair: string | null = null
    let conversionRate: number | null = null

    if (quoteCurrency === 'USD') {
      // Simple: pip value is always $10 per standard lot
      pipValue = 10
    } else {
      // Need to fetch the USD conversion rate
      conversionPair = CONVERSION_PAIR[quoteCurrency]

      if (!conversionPair) {
        return this.errorResult(pair, riskUsd, slPips, `❌ Could not determine conversion pair for <b>${upperPair}</b>.`)
      }

      this.logger.log(`Fetching ${conversionPair} for pip value conversion...`)
      const { price: rate } = await this.marketData.getCurrentPrice(conversionPair, apiKey)

      if (rate === null) {
        return this.errorResult(
          pair, riskUsd, slPips,
          `❌ Could not fetch live rate for <b>${conversionPair}</b>.\n\nPlease check your Twelve Data API key or try again shortly.`,
        )
      }

      conversionRate = rate

      // Quote stronger than USD (CHF, JPY, CAD) → divide
      // Quote weaker than USD (AUD, NZD, GBP, EUR) → multiply
      const divisionCurrencies = ['JPY', 'CHF', 'CAD']

      if (quoteCurrency === 'JPY') {
        // JPY pip size is 0.01 so we use 1000 instead of 10
        pipValue = 1000 / rate
      } else if (divisionCurrencies.includes(quoteCurrency)) {
        pipValue = 10 / rate
      } else {
        // AUD, NZD, GBP, EUR — multiply
        pipValue = 10 * rate
      }
    }

    // ── Final lot size calculation ────────────────────────────────
    // Lot size = Risk$ ÷ (SL pips × Pip value per std lot)
    const lotSize = riskUsd / (slPips * pipValue)
    const rounded = Math.round(lotSize * 100) / 100  // 2 decimal places
    const miniLots = Math.round(lotSize * 10 * 10) / 10   // 1 decimal
    const microLots = Math.round(lotSize * 100 * 10) / 10  // 1 decimal

    // ── Build response message ────────────────────────────────────
    const conversionLine = conversionPair
      ? `\n📡 <b>${conversionPair} Rate:</b> ${conversionRate.toFixed(5)}`
      : ''

    const message =
      `💰 <b>Lot Size — ${upperPair}</b>\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `📊 <b>Pair:</b> ${upperPair}\n` +
      `💵 <b>Risk:</b> $${riskUsd.toFixed(2)}\n` +
      `🛑 <b>SL:</b> ${slPips} pip${slPips !== 1 ? 's' : ''}\n` +
      `🎯 <b>Pip Value:</b> $${pipValue.toFixed(4)}${conversionLine}\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `📦 <b>Lot Size:   ${rounded} lots</b>\n` +
      `Mini lots:  ${miniLots}\n` +
      `Micro lots: ${microLots}\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `<i>Rate fetched live at time of calculation</i>`

    return {
      success: true,
      pair: upperPair,
      riskUsd,
      slPips,
      pipValue,
      conversionPair,
      conversionRate,
      lotSize: rounded,
      miniLots,
      microLots,
      message,
    }
  }

  // ── Supported pairs list message ──────────────────────────────

  getSupportedPairsMessage(): string {
    const usd = ['EURUSD', 'GBPUSD', 'AUDUSD', 'NZDUSD', 'XAUUSD']
    const jpy = ['USDJPY', 'EURJPY', 'GBPJPY', 'AUDJPY', 'NZDJPY', 'CADJPY', 'CHFJPY']
    const chf = ['USDCHF', 'EURCHF', 'GBPCHF', 'AUDCHF', 'NZDCHF', 'CADCHF']
    const cad = ['USDCAD', 'EURCAD', 'GBPCAD', 'AUDCAD', 'NZDCAD']
    const others = ['EURAUD', 'GBPAUD', 'EURNZD', 'GBPNZD', 'AUDNZD', 'EURGBP']

    return (
      `📋 <b>Supported Pairs</b>\n\n` +
      `<b>USD Quoted</b> (pip = $10 flat)\n` +
      `${usd.join(' • ')}\n\n` +
      `<b>JPY Quoted</b>\n` +
      `${jpy.join(' • ')}\n\n` +
      `<b>CHF Quoted</b>\n` +
      `${chf.join(' • ')}\n\n` +
      `<b>CAD Quoted</b>\n` +
      `${cad.join(' • ')}\n\n` +
      `<b>Other Crosses</b>\n` +
      `${others.join(' • ')}`
    )
  }

  private errorResult(
    pair: string,
    riskUsd: number,
    slPips: number,
    message: string,
  ): LotSizeResult {
    return {
      success: false,
      pair,
      riskUsd,
      slPips,
      pipValue: 0,
      conversionPair: null,
      conversionRate: null,
      lotSize: 0,
      miniLots: 0,
      microLots: 0,
      message,
    }
  }
}
