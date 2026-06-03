import { Injectable, Logger } from '@nestjs/common'
import { Cron } from '@nestjs/schedule'
import { PriceAlertService } from './price-alert.service'
import { UsersService } from '../users/users.service'
import { TelegramBotService } from '../telegram-bot/telegram-bot.service'

@Injectable()
export class PriceAlertCron {
  private readonly logger = new Logger(PriceAlertCron.name)

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

    // Process each user independently — their own API key, their own quota
    for (const user of users) {
      try {
        const triggered = await this.priceAlertService.checkAlertsForUser(
          user.chatId,
          user.twelveDataApiKey,
        )

        for (const { alert, currentPrice } of triggered) {
          const message = this.priceAlertService.buildAlertMessage(alert, currentPrice)
          await this.telegramBot.sendMessageToUser(user.chatId, message)
        }
      } catch (error) {
        this.logger.error(`[CRON] Error checking user ${user.chatId}: ${error.message}`)
      }
    }

    this.logger.log('[CRON] Price alert check complete')
  }
}
