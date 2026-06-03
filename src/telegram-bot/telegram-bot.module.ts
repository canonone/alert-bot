import { Module, forwardRef } from '@nestjs/common'
import { TelegramBotService } from './telegram-bot.service'
import { TelegramBotController } from './telegram-bot.controller'
import { PriceAlertModule } from '../price-alert/price-alert.module'
import { LotSizeModule } from '../lot-size/lot-size.module'
import { UsersModule } from '../users/users.module'
import { MarketDataModule } from '../market-data/market-data.module'
import { InviteModule } from '../invite/invite.module'

@Module({
  imports: [
    forwardRef(() => PriceAlertModule),
    LotSizeModule,
    UsersModule,
    MarketDataModule,
    InviteModule,
  ],
  controllers: [TelegramBotController],
  providers: [TelegramBotService],
  exports: [TelegramBotService],
})
export class TelegramBotModule {}
