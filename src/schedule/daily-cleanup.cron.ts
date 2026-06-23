import { Injectable, Logger, Inject, forwardRef } from '@nestjs/common'
import { Cron } from '@nestjs/schedule'
import { PriceAlertService } from '../price-alert/price-alert.service'
import { TelegramBotService } from '../telegram-bot/telegram-bot.service'
import { UsersService } from '../users/users.service'

@Injectable()
export class DailyCleanupCron {
  private readonly logger = new Logger(DailyCleanupCron.name)

  constructor(
    @Inject(forwardRef(() => PriceAlertService))
    private readonly priceAlertService: PriceAlertService,
    @Inject(forwardRef(() => TelegramBotService))
    private readonly telegramBot: TelegramBotService,
    private readonly usersService: UsersService,
  ) {}

  @Cron('0 21 * * *', { timeZone: 'UTC' })
  async runDailyCleanup() {
    this.logger.log('[DailyCleanup] Running daily alert cleanup at 10 PM WAT')

    const priceCounts = await this.priceAlertService.cancelAllAlertsForAllUsers()
    let totalCancelled = 0

    for (const [chatId, { priceCount }] of priceCounts) {
      totalCancelled += priceCount

      if (priceCount > 0) {
        const message =
          `🌙 <b>Daily Alert Cleanup</b>\n\n` +
          `All your active alerts have been automatically cancelled for end of day.\n\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `🔔 Price alerts cancelled: ${priceCount}\n` +
          `━━━━━━━━━━━━━━━━━━━━\n` +
          `<i>Set new alerts tomorrow with /setalert</i>`

        await this.telegramBot.sendMessageToUser(chatId, message)
      }
    }

    this.logger.log(
      `[DailyCleanup] Total alerts cancelled: ${totalCancelled} across ${priceCounts.size} user(s)`,
    )
  }

  @Cron('0 23 * * *', { timeZone: 'UTC' })
  async resetAlertIdCounters() {
    await this.usersService.resetAllAlertIdCounters()
    this.logger.log('[DailyCleanup] Alert ID counters reset for all users')
  }
}
