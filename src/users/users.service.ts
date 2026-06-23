import { Injectable, Logger } from '@nestjs/common'
import { InjectRepository } from '@nestjs/typeorm'
import { ConfigService } from '@nestjs/config'
import { Repository } from 'typeorm'
import { User } from './user.entity'

@Injectable()
export class UsersService {
  private readonly logger = new Logger(UsersService.name)
  private readonly adminChatId: string

  constructor(
    @InjectRepository(User)
    private readonly userRepo: Repository<User>,
    private readonly configService: ConfigService,
  ) {
    this.adminChatId = this.configService.getOrThrow('ADMIN_CHAT_ID')
  }

  // ── Find or create user on first contact ─────────────────────

  async findOrCreate(chatId: string, firstName?: string, username?: string): Promise<User> {
    let user = await this.userRepo.findOne({ where: { chatId } })
    const isAdmin = chatId === this.adminChatId

    if (!user) {
      user = this.userRepo.create({
        chatId,
        firstName: firstName ?? null,
        username: username ?? null,
        isApproved: isAdmin, // admin is auto-approved
        isAdmin,
        isSetup: false,
      })
      await this.userRepo.save(user)
      this.logger.log(`New user: ${chatId} (@${username ?? 'unknown'}) admin=${isAdmin}`)
    } else if (isAdmin && !user.isAdmin) {
      // Retroactively mark as admin if ADMIN_CHAT_ID was set after first contact
      await this.userRepo.update({ chatId }, { isAdmin: true, isApproved: true })
      user.isAdmin = true
      user.isApproved = true
    }

    return user
  }

  // ── Save API key ──────────────────────────────────────────────

  async saveApiKey(chatId: string, apiKey: string): Promise<User> {
    await this.userRepo.update({ chatId }, { twelveDataApiKey: apiKey, isSetup: true })
    return this.userRepo.findOne({ where: { chatId } })
  }

  // ── Look up a user ────────────────────────────────────────────

  async findByChatId(chatId: string): Promise<User | null> {
    return this.userRepo.findOne({ where: { chatId } })
  }

  // ── Get all setup users (for cron) ────────────────────────────

  async findAllSetupUsers(): Promise<User[]> {
    return this.userRepo.find({ where: { isSetup: true, isApproved: true } })
  }

  // ── Reset API key ─────────────────────────────────────────────

  async resetUser(chatId: string): Promise<void> {
    await this.userRepo.update({ chatId }, { twelveDataApiKey: null, isSetup: false })
  }

  // ── Per-user alert ID counter ─────────────────────────────────

  async getNextAlertId(chatId: string): Promise<number> {
    await this.userRepo.increment({ chatId }, 'alertIdCounter', 1)
    const user = await this.userRepo.findOne({ where: { chatId } })
    return user.alertIdCounter
  }

  async resetAllAlertIdCounters(): Promise<void> {
    await this.userRepo
      .createQueryBuilder()
      .update()
      .set({ alertIdCounter: 0 })
      .where('1=1')
      .execute()
  }

  // ── Check admin ───────────────────────────────────────────────

  isAdminChatId(chatId: string): boolean {
    return chatId === this.adminChatId
  }
}
