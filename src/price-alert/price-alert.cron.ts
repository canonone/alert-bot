import { Injectable, Logger } from '@nestjs/common'
import { Cron } from '@nestjs/schedule'
import { PriceAlertService } from './price-alert.service'
import { UsersService } from '../users/users.service'
import { TelegramBotService } from '../telegram-bot/telegram-bot.service'

@Injectable()
export class PriceAlertCron {
  private readonly logger = new Logger(PriceAlertCron.name)

  // key: chatId, value: UTC date string (YYYY-MM-DD) of last quota notification
  private readonly quotaNotifiedToday = new Map<string, string>()

  constructor(
    private readonly priceAlertService: PriceAlertService,
    private readonly usersService: UsersService,
    private readonly telegramBot: TelegramBotService,
  ) {}

  // Runs every 15 minutes
  @Cron('*/15 * * * *', { timeZone: 'UTC' })
  async checkAllUserAlerts() {
    this.logger.log('[CRON] Price alert check triggered')

    const users = await this.usersService.findAllSetupUsers()

    if (users.length === 0) {
      this.logger.log('[CRON] No setup users found, skipping')
      return
    }

    this.logger.log(`[CRON] Checking alerts for ${users.length} user(s)`)

    const todayUtc = new Date().toISOString().slice(0, 10)

    // Process each user independently — their own API key, their own quota
    for (const user of users) {
      try {
        const { triggered, quotaExceeded } = await this.priceAlertService.checkAlertsForUser(
          user.chatId,
          user.twelveDataApiKey,
        )

        if (quotaExceeded) {
          if (this.quotaNotifiedToday.get(user.chatId) !== todayUtc) {
            this.quotaNotifiedToday.set(user.chatId, todayUtc)
            await this.telegramBot.sendMessageToUser(
              user.chatId,
              `⚠️ <b>Daily API Limit Reached</b>\n\n` +
              `Your Twelve Data free key has hit the 800 requests/day limit.\n\n` +
              `Price checks for your alerts are paused until midnight UTC when your quota resets automatically.\n\n` +
              `━━━━━━━━━━━━━━━━━━━━\n` +
              `To avoid this, upgrade your plan at:\n` +
              `<a href="https://twelvedata.com/pricing">twelvedata.com/pricing</a>\n\n` +
              `Your alerts are safe and will resume automatically at midnight UTC 🕛`,
            )
          }
          continue
        }

        for (const { alert, currentPrice } of triggered) {
          const message = this.priceAlertService.buildAlertMessage(alert, currentPrice)
          await this.telegramBot.sendMessageToUser(user.chatId, message)
        }
      } catch (error) {
        this.logger.error(`[CRON] Error checking user ${user.chatId}: ${(error as Error).message}`)
      }
    }

    this.logger.log('[CRON] Price alert check complete')
  }

  // Clears quota notification tracking at midnight UTC so users can be re-notified the next day
  @Cron('0 0 * * *', { timeZone: 'UTC' })
  clearQuotaNotifications() {
    this.quotaNotifiedToday.clear()
    this.logger.log('[CRON] Quota notification tracking cleared for new day')
  }
}
