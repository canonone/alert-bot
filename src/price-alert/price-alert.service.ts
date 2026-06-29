import { Injectable, Logger } from '@nestjs/common'
import { InjectRepository } from '@nestjs/typeorm'
import { Repository } from 'typeorm'
import { PriceAlert, AlertType } from './price-alert.entity'
import { MarketDataService } from '../market-data/market-data.service'
import { UsersService } from '../users/users.service'

const ALLOWED_SYMBOLS = new Set([
  'AUDCAD', 'AUDCHF', 'AUDJPY', 'AUDNZD', 'AUDUSD',
  'CADCHF', 'CADJPY', 'CHFJPY',
  'EURAUD', 'EURCAD', 'EURCHF', 'EURGBP', 'EURJPY',
  'EURNZD', 'EURUSD',
  'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPJPY', 'GBPNZD', 'GBPUSD',
  'NZDCAD', 'NZDCHF', 'NZDJPY', 'NZDUSD',
  'USDCAD', 'USDCHF', 'USDJPY',
  'XAUUSD',
])

@Injectable()
export class PriceAlertService {
  private readonly logger = new Logger(PriceAlertService.name)

  constructor(
    @InjectRepository(PriceAlert)
    private readonly alertRepo: Repository<PriceAlert>,
    private readonly marketData: MarketDataService,
    private readonly usersService: UsersService,
  ) {}

  // ── Add alert ─────────────────────────────────────────────────

  async addAlert(
    chatId: string,
    symbol: string,
    type: AlertType,
    targetPrice: number,
  ): Promise<{ success: boolean; message: string }> {
    const upperSymbol = symbol.toUpperCase()

    if (!ALLOWED_SYMBOLS.has(upperSymbol)) {
      return {
        success: false,
        message:
          `❌ <b>${upperSymbol}</b> is not supported.\n\n` +
          `Use /pairs to see all supported symbols.`,
      }
    }

    if (isNaN(targetPrice) || targetPrice <= 0) {
      return {
        success: false,
        message: `❌ Invalid price <b>${targetPrice}</b>. Enter a valid positive number.`,
      }
    }

    const userAlertId = await this.usersService.getNextAlertId(chatId)

    const alert = this.alertRepo.create({
      chatId,
      symbol: upperSymbol,
      type,
      targetPrice,
      userAlertId,
      active: true,
    })

    await this.alertRepo.save(alert)
    const emoji = this.getEmoji(type)

    return {
      success: true,
      message:
        `${emoji} <b>Alert Set!</b>\n\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `📊 Symbol: <b>${upperSymbol}</b>\n` +
        `📌 Type: <b>${type}</b>\n` +
        `💰 Target Price: <b>${targetPrice}</b>\n` +
        `🔢 Alert ID: <b>#${userAlertId}</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `You'll be notified when price reaches this level.\n` +
        `Real-time alerts via live price feed ⚡`,
    }
  }

  // ── Cancel alert by ID (scoped to user) ───────────────────────

  async cancelAlert(
    chatId: string,
    id: number,
  ): Promise<{ success: boolean; message: string }> {
    const alert = await this.alertRepo.findOne({
      where: { userAlertId: id, chatId, active: true },
    })

    if (!alert) {
      return {
        success: false,
        message: `❌ No active alert found with ID <b>#${id}</b>\n\nUse /listalerts to see your alerts.`,
      }
    }

    await this.alertRepo.update({ id: alert.id }, { active: false })

    return {
      success: true,
      message:
        `✅ <b>Alert Cancelled</b>\n\n` +
        `ID: #${id}\n` +
        `Symbol: ${alert.symbol}\n` +
        `Type: ${alert.type}\n` +
        `Price: ${alert.targetPrice}`,
    }
  }

  // ── Cancel all alerts for a symbol (scoped to user) ──────────

  async cancelAlertsBySymbol(
    chatId: string,
    symbol: string,
  ): Promise<{ success: boolean; message: string }> {
    const upperSymbol = symbol.toUpperCase()

    const result = await this.alertRepo.update(
      { chatId, symbol: upperSymbol, active: true },
      { active: false },
    )

    const count = result.affected ?? 0

    if (count === 0) {
      return {
        success: false,
        message: `❌ No active alerts found for <b>${upperSymbol}</b>`,
      }
    }

    return {
      success: true,
      message: `✅ Cancelled <b>${count}</b> alert(s) for <b>${upperSymbol}</b>`,
    }
  }

  // ── Cancel all user alerts ────────────────────────────────────

  async cancelAllAlerts(chatId: string): Promise<{ success: boolean; message: string }> {
    const result = await this.alertRepo.update(
      { chatId, active: true },
      { active: false },
    )

    const count = result.affected ?? 0

    if (count === 0) {
      return { success: false, message: `❌ You have no active alerts to cancel.` }
    }

    return {
      success: true,
      message: `✅ Cancelled all <b>${count}</b> active alert(s).`,
    }
  }

  // ── List all active alerts for a user ─────────────────────────

  async listAlerts(chatId: string): Promise<string> {
    const active = await this.alertRepo.find({
      where: { chatId, active: true },
      order: { symbol: 'ASC', createdAt: 'ASC' },
    })

    if (active.length === 0) {
      return (
        `📭 <b>No Active Alerts</b>\n\n` +
        `Use /setalert to create one.\n\n` +
        `Example:\n` +
        `<code>/setalert GBPUSD SL 1.3200</code>`
      )
    }

    // Group by symbol
    const grouped = active.reduce((acc, alert) => {
      if (!acc[alert.symbol]) acc[alert.symbol] = []
      acc[alert.symbol].push(alert)
      return acc
    }, {} as Record<string, PriceAlert[]>)

    let message = `📋 <b>Your Active Alerts (${active.length})</b>\n\n`

    for (const [symbol, symbolAlerts] of Object.entries(grouped)) {
      message += `━━━━━━━━━━━━━━━━━━━━\n`
      message += `📊 <b>${symbol}</b>\n`
      for (const a of symbolAlerts) {
        message += `  ${this.getEmoji(a.type)} ${a.type} @ <b>${a.targetPrice}</b> — #<b>${a.userAlertId}</b>\n`
      }
    }

    message += `━━━━━━━━━━━━━━━━━━━━\n`
    message += `/cancelalert [id] — cancel by ID\n`
    message += `/cancelalerts [symbol] — cancel all for pair\n`
    message += `/cancelalerts all — cancel everything`

    return message
  }

  // ── WebSocket: get all active alerts grouped by symbol ────────

  async getActiveAlertsGroupedBySymbol(): Promise<Map<string, PriceAlert[]>> {
    const active = await this.alertRepo.find({ where: { active: true } })
    const grouped = new Map<string, PriceAlert[]>()
    for (const alert of active) {
      if (!grouped.has(alert.symbol)) grouped.set(alert.symbol, [])
      grouped.get(alert.symbol)!.push(alert)
    }
    return grouped
  }

  // ── WebSocket: check a single tick against all alerts for a symbol

  async checkTickAgainstAlerts(
    symbol: string,
    price: number,
    previousPrice: number | null,
  ): Promise<Array<{ alert: PriceAlert; currentPrice: number }>> {
    const active = await this.alertRepo.find({ where: { symbol, active: true } })
    if (active.length === 0) return []

    const triggered: Array<{ alert: PriceAlert; currentPrice: number }> = []

    for (const alert of active) {
      if (this.isTriggered(alert, price, previousPrice)) {
        await this.alertRepo.update({ id: alert.id }, { active: false })
        triggered.push({ alert, currentPrice: price })
      }
    }

    return triggered
  }

  // ── Daily cleanup: cancel all active alerts system-wide ───────

  async cancelAllAlertsForAllUsers(): Promise<Map<string, { priceCount: number }>> {
    const activeAlerts = await this.alertRepo.find({ where: { active: true } })

    const countMap = new Map<string, { priceCount: number }>()
    for (const alert of activeAlerts) {
      const current = countMap.get(alert.chatId) ?? { priceCount: 0 }
      current.priceCount++
      countMap.set(alert.chatId, current)
    }

    if (activeAlerts.length > 0) {
      await this.alertRepo.update({ active: true }, { active: false })
    }

    return countMap
  }

  // ── Cron: check all alerts for a specific user ────────────────

  async checkAlertsForUser(
    chatId: string,
    apiKey: string,
  ): Promise<{ triggered: Array<{ alert: PriceAlert; currentPrice: number }>; quotaExceeded: boolean }> {
    const active = await this.alertRepo.find({ where: { chatId, active: true } })
    if (active.length === 0) return { triggered: [], quotaExceeded: false }

    const symbols = [...new Set(active.map((a) => a.symbol))]
    this.logger.log(`[${chatId}] Checking ${symbols.length} symbol(s)...`)

    // Batch fetch all prices in one API call
    const { prices, quotaExceeded } = await this.marketData.getBatchPrices(symbols, apiKey)

    if (quotaExceeded) {
      this.logger.warn(`[${chatId}] Quota exceeded`)
      return { triggered: [], quotaExceeded: true }
    }

    const triggered: Array<{ alert: PriceAlert; currentPrice: number }> = []

    for (const alert of active) {
      const currentPrice = prices[alert.symbol]
      if (currentPrice == null) {
        this.logger.warn(`[${chatId}] No price for ${alert.symbol}`)
        continue
      }

      if (this.isTriggered(alert, currentPrice, null)) {
        await this.alertRepo.update({ id: alert.id }, { active: false })
        triggered.push({ alert, currentPrice })
        this.logger.log(
          `[${chatId}] 🔔 #${alert.id} triggered — ${alert.symbol} ${alert.type} @ ${alert.targetPrice} (current: ${currentPrice})`,
        )
      }
    }

    return { triggered, quotaExceeded: false }
  }

  // ── Alert trigger logic ───────────────────────────────────────

  isTriggered(alert: PriceAlert, currentPrice: number, previousPrice: number | null): boolean {
    switch (alert.type) {
      case 'SL':
        return currentPrice <= Number(alert.targetPrice)
      case 'TP':
        return currentPrice >= Number(alert.targetPrice)
      case 'TARGET': {
        const target = Number(alert.targetPrice)

        if (previousPrice !== null) {
          const crossedUp = previousPrice < target && currentPrice >= target
          const crossedDown = previousPrice > target && currentPrice <= target
          return crossedUp || crossedDown
        }

        return currentPrice === target
      }
    }
  }

  // ── Build alert notification message ─────────────────────────

  buildAlertMessage(alert: PriceAlert, currentPrice: number): string {
    const watTime = new Date(Date.now() + 60 * 60 * 1000)
    const formattedTime = watTime.toISOString().replace('T', ' ').slice(0, 16) + ' WAT'

    switch (alert.type) {
      case 'SL':
        return (
          `🔴 <b>STOP LOSS HIT — ${alert.symbol}</b>\n\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `💀 <b>SL Level:</b> ${alert.targetPrice}\n` +
          `📉 <b>Current Price:</b> ${currentPrice}\n` +
          `🕐 <b>Time:</b> ${formattedTime}\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `⚠️ Cut your losses. Protect your capital.`
        )
      case 'TP':
        return (
          `🟢 <b>TAKE PROFIT HIT — ${alert.symbol}</b>\n\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `🎯 <b>TP Level:</b> ${alert.targetPrice}\n` +
          `📈 <b>Current Price:</b> ${currentPrice}\n` +
          `🕐 <b>Time:</b> ${formattedTime}\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `💰 Well done. Lock in those gains.`
        )
      case 'TARGET':
        return (
          `🎯 <b>TARGET REACHED — ${alert.symbol}</b>\n\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `📍 <b>Target:</b> ${alert.targetPrice}\n` +
          `💰 <b>Current Price:</b> ${currentPrice}\n` +
          `🕐 <b>Time:</b> ${formattedTime}\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `📊 Your target level has been reached.`
        )
    }
  }

  getEmoji(type: AlertType): string {
    switch (type) {
      case 'SL': return '🔴'
      case 'TP': return '🟢'
      case 'TARGET': return '🎯'
    }
  }
}
