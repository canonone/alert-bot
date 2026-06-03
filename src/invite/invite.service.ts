import { Injectable, Logger } from '@nestjs/common'
import { InjectRepository } from '@nestjs/typeorm'
import { Repository } from 'typeorm'
import { InviteCode } from './invite-code.entity'
import { User } from '../users/user.entity'

@Injectable()
export class InviteService {
  private readonly logger = new Logger(InviteService.name)

  constructor(
    @InjectRepository(InviteCode)
    private readonly codeRepo: Repository<InviteCode>,
    @InjectRepository(User)
    private readonly userRepo: Repository<User>,
  ) {}

  // ── Generate N invite codes ───────────────────────────────────

  async generateCodes(count: number): Promise<string[]> {
    const codes: string[] = []

    for (let i = 0; i < count; i++) {
      const code = this.makeCode()
      const entity = this.codeRepo.create({ code, used: false })
      await this.codeRepo.save(entity)
      codes.push(code)
    }

    this.logger.log(`Generated ${count} invite code(s)`)
    return codes
  }

  // ── Redeem a code ─────────────────────────────────────────────

  async redeemCode(
    code: string,
    chatId: string,
  ): Promise<{ success: boolean; message: string }> {
    const upper = code.toUpperCase().trim()

    // Check if user is already approved
    const user = await this.userRepo.findOne({ where: { chatId } })
    if (user?.isApproved) {
      return {
        success: true,
        message: `✅ You already have access. Run /setup to continue.`,
      }
    }

    const inviteCode = await this.codeRepo.findOne({ where: { code: upper } })

    if (!inviteCode) {
      return {
        success: false,
        message:
          `❌ <b>Invalid invite code.</b>\n\n` +
          `Double-check the code and try again, or contact your admin for a new one.`,
      }
    }

    if (inviteCode.used) {
      return {
        success: false,
        message:
          `❌ <b>This invite code has already been used.</b>\n\n` +
          `Each code is single-use. Contact your admin for a new one.`,
      }
    }

    // Mark code as used
    await this.codeRepo.update(
      { id: inviteCode.id },
      { used: true, usedBy: chatId, usedAt: new Date() },
    )

    // Approve the user
    await this.userRepo.update({ chatId }, { isApproved: true })

    this.logger.log(`Code ${upper} redeemed by ${chatId}`)

    return {
      success: true,
      message:
        `✅ <b>Access Granted!</b>\n\n` +
        `Welcome to the team. You're now verified.\n\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `Next step — add your Twelve Data API key:\n\n` +
        `1️⃣ Get a free key at: <a href="https://twelvedata.com">twelvedata.com</a>\n` +
        `2️⃣ Send: <code>/setup YOUR_API_KEY</code>`,
    }
  }

  // ── List all codes (for admin) ────────────────────────────────

  async listCodes(): Promise<string> {
    const codes = await this.codeRepo.find({ order: { createdAt: 'DESC' } })

    if (codes.length === 0) {
      return `📭 No invite codes generated yet.\n\nUse <code>/gencode 5</code> to generate some.`
    }

    const unused = codes.filter(c => !c.used)
    const used = codes.filter(c => c.used)

    let msg = `📋 <b>Invite Codes</b>\n\n`

    if (unused.length > 0) {
      msg += `✅ <b>Available (${unused.length})</b>\n`
      for (const c of unused) {
        msg += `  <code>${c.code}</code>\n`
      }
      msg += `\n`
    }

    if (used.length > 0) {
      msg += `🔒 <b>Used (${used.length})</b>\n`
      for (const c of used) {
        msg += `  <code>${c.code}</code> — by ${c.usedBy}\n`
      }
    }

    return msg
  }

  // ── Revoke a code (admin) ─────────────────────────────────────

  async revokeCode(code: string): Promise<{ success: boolean; message: string }> {
    const upper = code.toUpperCase().trim()
    const result = await this.codeRepo.delete({ code: upper })

    if (result.affected === 0) {
      return { success: false, message: `❌ Code <code>${upper}</code> not found.` }
    }

    return { success: true, message: `✅ Code <code>${upper}</code> deleted.` }
  }

  // ── Code generator ────────────────────────────────────────────
  // Format: TRADE-XXXXX (5 random alphanumeric chars)

  private makeCode(): string {
    const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789' // no 0/O/1/I to avoid confusion
    let suffix = ''
    for (let i = 0; i < 5; i++) {
      suffix += chars[Math.floor(Math.random() * chars.length)]
    }
    return `TRADE-${suffix}`
  }
}
