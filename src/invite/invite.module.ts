import { Module } from '@nestjs/common'
import { TypeOrmModule } from '@nestjs/typeorm'
import { InviteCode } from './invite-code.entity'
import { InviteService } from './invite.service'
import { User } from '../users/user.entity'

@Module({
  imports: [TypeOrmModule.forFeature([InviteCode, User])],
  providers: [InviteService],
  exports: [InviteService],
})
export class InviteModule {}
