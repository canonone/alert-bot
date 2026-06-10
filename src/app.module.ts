import { Module } from '@nestjs/common'
import { ConfigModule, ConfigService } from '@nestjs/config'
import { ScheduleModule as NestScheduleModule } from '@nestjs/schedule'
import { TypeOrmModule } from '@nestjs/typeorm'
import { UsersModule } from './users/users.module'
import { MarketDataModule } from './market-data/market-data.module'
import { PriceAlertModule } from './price-alert/price-alert.module'
import { TelegramBotModule } from './telegram-bot/telegram-bot.module'
import { LotSizeModule } from './lot-size/lot-size.module'
import { InviteModule } from './invite/invite.module'
import { EmaAlertModule } from './ema-alert/ema-alert.module'
import { ScheduleModule } from './schedule/schedule.module'
import { User } from './users/user.entity'
import { PriceAlert } from './price-alert/price-alert.entity'
import { InviteCode } from './invite/invite-code.entity'
import { EmaAlert } from './ema-alert/ema-alert.entity'

@Module({
  imports: [
    ConfigModule.forRoot({ isGlobal: true }),
    NestScheduleModule.forRoot(),
    TypeOrmModule.forRootAsync({
      inject: [ConfigService],
      useFactory: (config: ConfigService) => ({
        type: 'postgres',
        host: config.getOrThrow('DB_HOST'),
        port: config.get<number>('DB_PORT', 5432),
        username: config.getOrThrow('DB_USERNAME'),
        password: config.getOrThrow('DB_PASSWORD'),
        database: config.getOrThrow('DB_NAME'),
        entities: [User, PriceAlert, InviteCode, EmaAlert],
        synchronize: true,
        logging: config.get('NODE_ENV') === 'development',
      }),
    }),
    UsersModule,
    MarketDataModule,
    LotSizeModule,
    PriceAlertModule,
    InviteModule,
    EmaAlertModule,
    ScheduleModule,
    TelegramBotModule,
  ],
})
export class AppModule {}
