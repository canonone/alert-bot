import { Module, forwardRef } from '@nestjs/common'
import { TypeOrmModule } from '@nestjs/typeorm'
import { PriceAlert } from './price-alert.entity'
import { PriceAlertService } from './price-alert.service'
import { PriceAlertCron } from './price-alert.cron'
import { MarketDataModule } from '../market-data/market-data.module'
import { UsersModule } from '../users/users.module'
import { TelegramBotModule } from '../telegram-bot/telegram-bot.module'

@Module({
  imports: [
    TypeOrmModule.forFeature([PriceAlert]),
    MarketDataModule,
    UsersModule,
    forwardRef(() => TelegramBotModule),
  ],
  providers: [PriceAlertService, PriceAlertCron],
  exports: [PriceAlertService],
})
export class PriceAlertModule {}
