import { Injectable, Logger, OnModuleInit, OnModuleDestroy, Inject, forwardRef } from '@nestjs/common'
import { ConfigService } from '@nestjs/config'
import * as WebSocket from 'ws'
import axios from 'axios'
import { EmaAlertService } from './ema-alert.service'
import { CandleBuilderService } from './candle-builder.service'
import { EmaCalculatorService } from './ema-calculator.service'
import { PriceAlertService } from '../price-alert/price-alert.service'
import { TelegramBotService } from '../telegram-bot/telegram-bot.service'

@Injectable()
export class FinnhubService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(FinnhubService.name)
  private ws: WebSocket | null = null
  private readonly subscribedSymbols = new Set<string>()
  private reconnectAttempts = 0
  private readonly maxReconnectAttempts = 10
  private readonly finnhubApiKey: string

  constructor(
    private readonly configService: ConfigService,
    private readonly emaAlertService: EmaAlertService,
    private readonly candleBuilder: CandleBuilderService,
    private readonly emaCalculator: EmaCalculatorService,
    private readonly priceAlertService: PriceAlertService,
    @Inject(forwardRef(() => TelegramBotService))
    private readonly telegramBot: TelegramBotService,
  ) {
    this.finnhubApiKey = this.configService.getOrThrow('FINNHUB_API_KEY')
  }

  async onModuleInit() {
    await this.seedHistoricalCandles()
    this.connect()
  }

  connect() {
    const url = `wss://ws.finnhub.io?token=${this.finnhubApiKey}`
    this.ws = new WebSocket(url)

    this.ws.on('open', () => {
      this.logger.log('[Finnhub] WebSocket connected')
      this.reconnectAttempts = 0
      this.subscribeToActiveSymbols()
    })

    this.ws.on('message', (data: WebSocket.RawData) => {
      this.handleMessage(data.toString())
    })

    this.ws.on('error', (error: Error) => {
      this.logger.error(`[Finnhub] WebSocket error: ${error.message}`)
    })

    this.ws.on('close', () => {
      this.logger.warn('[Finnhub] WebSocket disconnected')
      this.scheduleReconnect()
    })
  }

  private scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      this.logger.error('[Finnhub] Max reconnect attempts reached, giving up')
      return
    }
    const delayMs = 5000 * (this.reconnectAttempts + 1)
    this.reconnectAttempts++
    this.logger.log(`[Finnhub] Reconnecting in ${delayMs / 1000}s (attempt ${this.reconnectAttempts})`)
    setTimeout(() => this.connect(), delayMs)
  }

  private async subscribeToActiveSymbols() {
    const emaGrouped = await this.emaAlertService.getActiveAlertsGroupedBySymbolAndTimeframe()
    const emaSymbols = new Set<string>()
    for (const key of emaGrouped.keys()) {
      emaSymbols.add(key.split('_')[0])
    }

    const priceGrouped = await this.priceAlertService.getActiveAlertsGroupedBySymbol()
    const allSymbols = new Set<string>([...emaSymbols, ...priceGrouped.keys()])

    const newSymbols: string[] = []
    for (const symbol of allSymbols) {
      if (!this.subscribedSymbols.has(symbol)) {
        const finnhubSymbol = this.toFinnhubSymbol(symbol)
        this.ws?.send(JSON.stringify({ type: 'subscribe', symbol: finnhubSymbol }))
        this.subscribedSymbols.add(symbol)
        newSymbols.push(finnhubSymbol)
      }
    }

    if (newSymbols.length > 0) {
      this.logger.log(`[Finnhub] Subscribed to: ${newSymbols.join(', ')}`)
    } else {
      this.logger.log('[Finnhub] No new symbols to subscribe')
    }
  }

  subscribeToSymbol(symbol: string) {
    if (this.subscribedSymbols.has(symbol)) return
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const finnhubSymbol = this.toFinnhubSymbol(symbol)
    this.ws.send(JSON.stringify({ type: 'subscribe', symbol: finnhubSymbol }))
    this.subscribedSymbols.add(symbol)
    this.logger.log(`[Finnhub] Dynamically subscribed to: ${finnhubSymbol}`)
  }

  private handleMessage(data: string) {
    let parsed: any
    try {
      parsed = JSON.parse(data)
    } catch {
      return
    }

    if (parsed.type === 'trade') {
      for (const trade of parsed.data ?? []) {
        const symbol = this.fromFinnhubSymbol(trade.s)
        const price: number = trade.p
        const timestamp: number = trade.t

        this.checkPriceAlerts(symbol, price)

        for (const timeframe of ['5M', '30M']) {
          const completed = this.candleBuilder.processTick(symbol, timeframe, price, timestamp)
          if (completed) {
            this.checkEmaAlerts(symbol, timeframe)
          }
        }
      }
    } else if (parsed.type === 'error') {
      this.logger.error(`[Finnhub] API error: ${parsed.msg}`)
    }
  }

  private async checkEmaAlerts(symbol: string, timeframe: string) {
    const candles = this.candleBuilder.getCandles(symbol, timeframe)
    if (candles.length < 12) return

    const emaValues = this.emaCalculator.calculate(candles, 10)
    const cross = this.emaCalculator.detectCross(candles, emaValues)
    if (!cross.crossed || !cross.direction) return

    const alerts = await this.emaAlertService.getActiveAlertsForSymbolTimeframe(symbol, timeframe)
    const matching = alerts.filter((a) => a.direction === cross.direction)

    for (const alert of matching) {
      await this.emaAlertService.markAlertTriggered(alert.id)
      const message = this.emaAlertService.buildAlertMessage(alert, cross.currentPrice, cross.emaValue)
      await this.telegramBot.sendMessageToUser(alert.chatId, message)
      this.logger.log(
        `[Finnhub] EMA alert #${alert.id} triggered — ${symbol} ${timeframe} ${cross.direction} for ${alert.chatId}`,
      )
    }
  }

  private async checkPriceAlerts(symbol: string, price: number) {
    const triggered = await this.priceAlertService.checkTickAgainstAlerts(symbol, price)
    for (const { alert, currentPrice } of triggered) {
      const message = this.priceAlertService.buildAlertMessage(alert, currentPrice)
      await this.telegramBot.sendMessageToUser(alert.chatId, message)
      this.logger.log(
        `[Finnhub] Price alert #${alert.userAlertId} triggered — ${symbol} ${alert.type} @ ${alert.targetPrice} for ${alert.chatId}`,
      )
    }
  }

  async seedHistoricalCandles() {
    const grouped = await this.emaAlertService.getActiveAlertsGroupedBySymbolAndTimeframe()
    if (grouped.size === 0) {
      this.logger.log('[Finnhub] No active EMA alerts, skipping candle seed')
      return
    }

    let seededCount = 0

    for (const key of grouped.keys()) {
      const underscoreIdx = key.indexOf('_')
      const symbol = key.slice(0, underscoreIdx)
      const timeframe = key.slice(underscoreIdx + 1)
      const resolution = timeframe === '5M' ? '5' : '30'
      const finnhubSymbol = this.toFinnhubSymbol(symbol)

      try {
        const response = await axios.get('https://finnhub.io/api/v1/forex/candle', {
          params: { symbol: finnhubSymbol, resolution, count: 50, token: this.finnhubApiKey },
          timeout: 10000,
        })

        const { o, h, l, c, t, s } = response.data
        if (s !== 'ok' || !Array.isArray(t)) {
          this.logger.warn(`[Finnhub] No candle data for ${key}`)
          continue
        }

        const candles = (t as number[]).map((ts, i) => ({
          open: o[i], high: h[i], low: l[i], close: c[i],
          timestamp: ts * 1000, complete: true,
        }))

        this.candleBuilder.seedCandles(symbol, timeframe, candles)
        seededCount++
        this.logger.log(`[Finnhub] Seeded ${candles.length} candles for ${key}`)
      } catch (error) {
        this.logger.error(`[Finnhub] Failed to seed ${key}: ${(error as Error).message}`)
      }
    }

    this.logger.log(`[Finnhub] Seeded ${seededCount} symbol+timeframe combination(s)`)
  }

  onModuleDestroy() {
    this.ws?.close()
    this.logger.log('[Finnhub] WebSocket closed')
  }

  private toFinnhubSymbol(symbol: string): string {
    return `OANDA:${symbol.slice(0, 3)}_${symbol.slice(3)}`
  }

  private fromFinnhubSymbol(finnhubSymbol: string): string {
    return finnhubSymbol.replace('OANDA:', '').replace('_', '')
  }
}
