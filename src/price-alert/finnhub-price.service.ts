import { Injectable, Logger, OnModuleInit, OnModuleDestroy, Inject, forwardRef } from '@nestjs/common'
import { ConfigService } from '@nestjs/config'
import * as WebSocket from 'ws'
import { PriceAlertService } from './price-alert.service'
import { TelegramBotService } from '../telegram-bot/telegram-bot.service'

@Injectable()
export class FinnhubPriceService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(FinnhubPriceService.name)
  private ws: WebSocket | null = null
  private readonly subscribedSymbols = new Set<string>()
  private readonly lastPrice = new Map<string, number>()
  private reconnectAttempts = 0
  private readonly maxReconnectAttempts = 10
  private readonly finnhubApiKey: string

  constructor(
    private readonly configService: ConfigService,
    private readonly priceAlertService: PriceAlertService,
    @Inject(forwardRef(() => TelegramBotService))
    private readonly telegramBot: TelegramBotService,
  ) {
    this.finnhubApiKey = this.configService.getOrThrow('FINNHUB_API_KEY')
  }

  onModuleInit() {
    this.connect()
  }

  connect() {
    const url = `wss://ws.finnhub.io?token=${this.finnhubApiKey}`
    this.ws = new WebSocket(url)

    this.ws.on('open', () => {
      this.logger.log('[FinnhubPrice] WebSocket connected')
      this.reconnectAttempts = 0
      this.subscribedSymbols.clear()
      this.subscribeToActiveSymbols()
    })

    this.ws.on('message', (data: WebSocket.RawData) => {
      this.handleMessage(data.toString())
    })

    this.ws.on('error', (error: Error) => {
      this.logger.error(`[FinnhubPrice] WebSocket error: ${error.message}`)
    })

    this.ws.on('close', () => {
      this.logger.warn('[FinnhubPrice] WebSocket disconnected')
      this.scheduleReconnect()
    })
  }

  private scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      this.logger.error('[FinnhubPrice] Max reconnect attempts reached, giving up')
      return
    }
    const delayMs = 5000 * (this.reconnectAttempts + 1)
    this.reconnectAttempts++
    this.logger.log(`[FinnhubPrice] Reconnecting in ${delayMs / 1000}s (attempt ${this.reconnectAttempts})`)
    setTimeout(() => this.connect(), delayMs)
  }

  private async subscribeToActiveSymbols() {
    const grouped = await this.priceAlertService.getActiveAlertsGroupedBySymbol()
    const symbols: string[] = []

    for (const symbol of grouped.keys()) {
      const finnhubSymbol = this.toFinnhubSymbol(symbol)
      this.ws?.send(JSON.stringify({ type: 'subscribe', symbol: finnhubSymbol }))
      this.subscribedSymbols.add(symbol)
      symbols.push(finnhubSymbol)
    }

    if (symbols.length > 0) {
      this.logger.log(`[FinnhubPrice] Subscribed to: ${symbols.join(', ')}`)
    } else {
      this.logger.log('[FinnhubPrice] No active symbols to subscribe')
    }
  }

  subscribeToSymbol(symbol: string) {
    if (this.subscribedSymbols.has(symbol)) return
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const finnhubSymbol = this.toFinnhubSymbol(symbol)
    this.ws.send(JSON.stringify({ type: 'subscribe', symbol: finnhubSymbol }))
    this.subscribedSymbols.add(symbol)
    this.logger.log(`[FinnhubPrice] Dynamically subscribed to: ${finnhubSymbol}`)
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
        const previousPrice = this.lastPrice.get(symbol) ?? null
        this.lastPrice.set(symbol, price)
        this.checkPriceAlerts(symbol, price, previousPrice)
      }
    } else if (parsed.type === 'error') {
      this.logger.error(`[FinnhubPrice] API error: ${parsed.msg}`)
    }
  }

  private async checkPriceAlerts(symbol: string, price: number, previousPrice: number | null) {
    const triggered = await this.priceAlertService.checkTickAgainstAlerts(symbol, price, previousPrice)
    for (const { alert, currentPrice } of triggered) {
      const message = this.priceAlertService.buildAlertMessage(alert, currentPrice)
      await this.telegramBot.sendMessageToUser(alert.chatId, message)
      this.logger.log(
        `[FinnhubPrice] Price alert #${alert.userAlertId} triggered — ${symbol} ${alert.type} @ ${alert.targetPrice} for ${alert.chatId}`,
      )
    }
  }

  onModuleDestroy() {
    this.ws?.close()
    this.logger.log('[FinnhubPrice] WebSocket closed')
  }

  private toFinnhubSymbol(symbol: string): string {
    return `OANDA:${symbol.slice(0, 3)}_${symbol.slice(3)}`
  }

  private fromFinnhubSymbol(finnhubSymbol: string): string {
    return finnhubSymbol.replace('OANDA:', '').replace('_', '')
  }
}
