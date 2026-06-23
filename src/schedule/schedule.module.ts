import { Module, forwardRef } from '@nestjs/common'
import { DailyCleanupCron } from './daily-cleanup.cron'
import { PriceAlertModule } from '../price-alert/price-alert.module'
import { TelegramBotModule } from '../telegram-bot/telegram-bot.module'
import { UsersModule } from '../users/users.module'

@Module({
  imports: [
    forwardRef(() => PriceAlertModule),
    forwardRef(() => TelegramBotModule),
    UsersModule,
  ],
  providers: [DailyCleanupCron],
})
export class ScheduleModule {}
