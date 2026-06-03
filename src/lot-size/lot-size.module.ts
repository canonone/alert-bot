import { Module } from '@nestjs/common'
import { LotSizeService } from './lot-size.service'
import { MarketDataModule } from '../market-data/market-data.module'

@Module({
  imports: [MarketDataModule],
  providers: [LotSizeService],
  exports: [LotSizeService],
})
export class LotSizeModule {}
