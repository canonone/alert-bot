import { Injectable, Logger } from '@nestjs/common'
import { InjectRepository } from '@nestjs/typeorm'
import { Repository } from 'typeorm'
import { EmaAlert } from './ema-alert.entity'
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
export class EmaAlertService {
  private readonly logger = new Logger(EmaAlertService.name)

  constructor(
    @InjectRepository(EmaAlert)
    private readonly alertRepo: Repository<EmaAlert>,
    private readonly usersService: UsersService,
  ) {}

  async addAlert(
    chatId: string,
    symbol: string,
    timeframe: string,
    direction: string,
  ): Promise<{ success: boolean; message: string }> {
    const upperSymbol = symbol.toUpperCase()
    const upperTimeframe = timeframe.toUpperCase()
    const lowerDirection = direction.toLowerCase()

    if (!ALLOWED_SYMBOLS.has(upperSymbol)) {
      return {
        success: false,
        message: `❌ <b>${upperSymbol}</b> is not supported.\n\nUse /pairs to see all supported symbols.`,
      }
    }

    if (!['5M', '30M'].includes(upperTimeframe)) {
      return {
        success: false,
        message: `❌ Invalid timeframe <b>${timeframe}</b>.\n\nValid timeframes: <b>5M</b> | <b>30M</b>`,
      }
    }

    if (!['cross_up', 'cross_down'].includes(lowerDirection)) {
      return {
        success: false,
        message: `❌ Invalid direction <b>${direction}</b>.\n\nValid directions: <b>cross_up</b> | <b>cross_down</b>`,
      }
    }

    const userAlertId = await this.usersService.getNextAlertId(chatId)

    const alert = this.alertRepo.create({
      chatId,
      symbol: upperSymbol,
      emaLength: 10,
      timeframe: upperTimeframe,
      direction: lowerDirection,
      userAlertId,
      active: true,
    })

    await this.alertRepo.save(alert)
    const directionLabel = lowerDirection === 'cross_up' ? 'Cross Up' : 'Cross Down'
    const crossVerb = lowerDirection === 'cross_up' ? 'above' : 'below'

    return {
      success: true,
      message:
        `✅ <b>EMA Alert Set!</b>\n\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `📊 Pair: <b>${upperSymbol}</b>\n` +
        `📈 EMA: <b>10</b>\n` +
        `⏱ Timeframe: <b>${upperTimeframe}</b>\n` +
        `🎯 Direction: <b>${directionLabel}</b>\n` +
        `🔢 Alert ID: <b>#${userAlertId}</b>\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `You'll be notified when price crosses ${crossVerb} EMA(10) on the ${upperTimeframe} chart.`,
    }
  }

  async listAlerts(chatId: string): Promise<string> {
    const active = await this.alertRepo.find({
      where: { chatId, active: true },
      order: { symbol: 'ASC', createdAt: 'ASC' },
    })

    if (active.length === 0) {
      return (
        `📭 <b>No Active EMA Alerts</b>\n\n` +
        `Use /setema to create one.\n\n` +
        `Example:\n` +
        `<code>/setema GBPUSD 5M cross_up</code>`
      )
    }

    const grouped = active.reduce((acc, alert) => {
      if (!acc[alert.symbol]) acc[alert.symbol] = []
      acc[alert.symbol].push(alert)
      return acc
    }, {} as Record<string, EmaAlert[]>)

    let message = `📋 <b>Your Active EMA Alerts (${active.length})</b>\n\n`

    for (const [symbol, symbolAlerts] of Object.entries(grouped)) {
      message += `━━━━━━━━━━━━━━━━━━━━\n`
      message += `📊 <b>${symbol}</b>\n`
      for (const a of symbolAlerts) {
        const dirLabel = a.direction === 'cross_up' ? '↗ Cross Up' : '↘ Cross Down'
        message += `  ${dirLabel} · EMA(${a.emaLength}) · ${a.timeframe} — #<b>${a.userAlertId}</b>\n`
      }
    }

    message += `━━━━━━━━━━━━━━━━━━━━\n`
    message += `/cancelema [id] — cancel by ID\n`
    message += `/cancelemas [symbol] — cancel all for pair\n`
    message += `/cancelemas all — cancel everything`

    return message
  }

  async cancelAlert(
    chatId: string,
    id: number,
  ): Promise<{ success: boolean; message: string }> {
    const alert = await this.alertRepo.findOne({ where: { userAlertId: id, chatId, active: true } })

    if (!alert) {
      return {
        success: false,
        message: `❌ No active EMA alert found with ID <b>#${id}</b>\n\nUse /listemas to see your alerts.`,
      }
    }

    await this.alertRepo.update({ id: alert.id }, { active: false })

    return {
      success: true,
      message:
        `✅ <b>EMA Alert Cancelled</b>\n\n` +
        `ID: #${id}\n` +
        `Symbol: ${alert.symbol}\n` +
        `Timeframe: ${alert.timeframe}\n` +
        `Direction: ${alert.direction}`,
    }
  }

  async cancelBySymbol(
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
      return { success: false, message: `❌ No active EMA alerts found for <b>${upperSymbol}</b>` }
    }
    return { success: true, message: `✅ Cancelled <b>${count}</b> EMA alert(s) for <b>${upperSymbol}</b>` }
  }

  async cancelAllAlerts(chatId: string): Promise<{ success: boolean; message: string }> {
    const result = await this.alertRepo.update({ chatId, active: true }, { active: false })
    const count = result.affected ?? 0
    if (count === 0) {
      return { success: false, message: `❌ You have no active EMA alerts to cancel.` }
    }
    return { success: true, message: `✅ Cancelled all <b>${count}</b> active EMA alert(s).` }
  }

  async cancelAllAlertsForAllUsers(): Promise<Map<string, { emaCount: number }>> {
    const activeAlerts = await this.alertRepo.find({ where: { active: true } })

    const countMap = new Map<string, { emaCount: number }>()
    for (const alert of activeAlerts) {
      const current = countMap.get(alert.chatId) ?? { emaCount: 0 }
      current.emaCount++
      countMap.set(alert.chatId, current)
    }

    if (activeAlerts.length > 0) {
      await this.alertRepo.update({ active: true }, { active: false })
    }

    return countMap
  }

  async getActiveAlertsGroupedBySymbolAndTimeframe(): Promise<Map<string, EmaAlert[]>> {
    const active = await this.alertRepo.find({ where: { active: true } })
    const grouped = new Map<string, EmaAlert[]>()
    for (const alert of active) {
      const key = `${alert.symbol}_${alert.timeframe}`
      if (!grouped.has(key)) grouped.set(key, [])
      grouped.get(key)!.push(alert)
    }
    return grouped
  }

  async getActiveAlertsForSymbolTimeframe(symbol: string, timeframe: string): Promise<EmaAlert[]> {
    return this.alertRepo.find({ where: { symbol, timeframe, active: true } })
  }

  async markAlertTriggered(id: number): Promise<void> {
    await this.alertRepo.update({ id }, { active: false })
  }

  buildAlertMessage(alert: EmaAlert, currentPrice: number, emaValue: number): string {
    const watTime = new Date(Date.now() + 60 * 60 * 1000)
    const formattedTime = watTime.toISOString().replace('T', ' ').slice(0, 16) + ' WAT'
    const directionLabel = alert.direction === 'cross_up' ? 'Cross Up' : 'Cross Down'
    const crossVerb = alert.direction === 'cross_up' ? 'above' : 'below'

    return (
      `📊 <b>EMA CROSS — ${alert.symbol} ${alert.timeframe}</b>\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `📈 Direction: <b>${directionLabel}</b>\n` +
      `💰 Price: <b>${currentPrice}</b>\n` +
      `〰️ EMA(${alert.emaLength}): <b>${emaValue.toFixed(5)}</b>\n` +
      `🕐 Time: <b>${formattedTime}</b>\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `Price has crossed ${crossVerb} EMA(${alert.emaLength}) on the ${alert.timeframe} chart.`
    )
  }
}
