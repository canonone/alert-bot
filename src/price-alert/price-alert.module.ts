import { Module } from '@nestjs/common'
import { TypeOrmModule } from '@nestjs/typeorm'
import { PriceAlert } from './price-alert.entity'
import { PriceAlertService } from './price-alert.service'
import { MarketDataModule } from '../market-data/market-data.module'
import { UsersModule } from '../users/users.module'

@Module({
  imports: [
    TypeOrmModule.forFeature([PriceAlert]),
    MarketDataModule,
    UsersModule,
  ],
  providers: [PriceAlertService],
  exports: [PriceAlertService],
})
export class PriceAlertModule {}
