import { Module, forwardRef } from '@nestjs/common'
import { TypeOrmModule } from '@nestjs/typeorm'
import { EmaAlert } from './ema-alert.entity'
import { EmaAlertService } from './ema-alert.service'
import { CandleBuilderService } from './candle-builder.service'
import { EmaCalculatorService } from './ema-calculator.service'
import { FinnhubService } from './finnhub.service'
import { UsersModule } from '../users/users.module'
import { TelegramBotModule } from '../telegram-bot/telegram-bot.module'
import { PriceAlertModule } from '../price-alert/price-alert.module'

@Module({
  imports: [
    TypeOrmModule.forFeature([EmaAlert]),
    UsersModule,
    PriceAlertModule,
    forwardRef(() => TelegramBotModule),
  ],
  providers: [EmaAlertService, CandleBuilderService, EmaCalculatorService, FinnhubService],
  exports: [EmaAlertService, FinnhubService],
})
export class EmaAlertModule {}
