import { Injectable, Logger, OnModuleInit } from '@nestjs/common'
import { ConfigService } from '@nestjs/config'
import { PriceAlertService } from '../price-alert/price-alert.service'
import { LotSizeService } from '../lot-size/lot-size.service'
import { UsersService } from '../users/users.service'
import { MarketDataService } from '../market-data/market-data.service'
import { InviteService } from '../invite/invite.service'
import { AlertType } from '../price-alert/price-alert.entity'
import axios from 'axios'

@Injectable()
export class TelegramBotService implements OnModuleInit {
  private readonly logger = new Logger(TelegramBotService.name)
  private readonly botToken: string
  private readonly baseUrl: string
  private readonly webhookUrl: string
  private readonly isProduction: boolean

  constructor(
    private readonly configService: ConfigService,
    private readonly priceAlertService: PriceAlertService,
    private readonly lotSizeService: LotSizeService,
    private readonly usersService: UsersService,
    private readonly marketData: MarketDataService,
    private readonly inviteService: InviteService,
  ) {
    this.botToken = this.configService.getOrThrow('TELEGRAM_BOT_TOKEN')
    this.baseUrl = `https://api.telegram.org/bot${this.botToken}`
    this.webhookUrl = this.configService.get('WEBHOOK_URL', '')
    this.isProduction = this.configService.get('NODE_ENV') === 'production'
  }

  async onModuleInit() {
    if (this.isProduction && this.webhookUrl) {
      await this.registerWebhook()
    } else {
      this.logger.log('Development mode — using polling')
      this.startPolling()
    }
  }

  // ── Webhook ───────────────────────────────────────────────────

  private async registerWebhook(): Promise<void> {
    const url = `${this.webhookUrl}/webhook/${this.botToken}`
    try {
      await axios.post(`${this.baseUrl}/deleteWebhook`)
      const response = await axios.post(`${this.baseUrl}/setWebhook`, {
        url,
        allowed_updates: ['message'],
        drop_pending_updates: true,
      })
      if (response.data?.ok) {
        this.logger.log(`✅ Webhook registered: ${url}`)
      } else {
        this.logger.error(`❌ Webhook failed: ${JSON.stringify(response.data)}`)
      }
    } catch (error) {
      this.logger.error(`❌ Webhook error: ${error.message}`)
    }
  }

  async handleUpdate(update: any): Promise<void> {
    const message = update?.message
    if (!message?.text) return

    const chatId = String(message.chat.id)
    const firstName = message.from?.first_name ?? null
    const username = message.from?.username ?? null
    const text = message.text.trim()

    await this.usersService.findOrCreate(chatId, firstName, username)
    await this.handleCommand(chatId, text)
  }

  // ── Polling (dev only) ────────────────────────────────────────

  private startPolling() {
    let lastUpdateId = 0
    setInterval(async () => {
      try {
        const response = await axios.get(`${this.baseUrl}/getUpdates`, {
          params: { offset: lastUpdateId + 1, timeout: 1 },
          timeout: 10000,
        })
        const updates = response.data?.result
        if (!updates || updates.length === 0) return
        for (const update of updates) {
          lastUpdateId = update.update_id
          await this.handleUpdate(update)
        }
      } catch { }
    }, 3000)
  }

  // ── Command router ────────────────────────────────────────────

  private async handleCommand(chatId: string, text: string): Promise<void> {
    const parts = text.split(' ').filter(Boolean)
    const command = parts[0].toLowerCase()

    // These commands work without approval
    switch (command) {
      case '/start': return this.handleStart(chatId)
      case '/join':  return this.handleJoin(chatId, parts)
      case '/help':  return this.handleHelp(chatId)
    }

    // Admin-only commands
    if (this.usersService.isAdminChatId(chatId)) {
      switch (command) {
        case '/gencode':    return this.handleGenCode(chatId, parts)
        case '/listcodes':  return this.handleListCodes(chatId)
        case '/revokecode': return this.handleRevokeCode(chatId, parts)
      }
    }

    // All other commands require approval first
    const user = await this.usersService.findByChatId(chatId)
    if (!user?.isApproved) {
      await this.sendMessageToUser(
        chatId,
        `🔒 <b>Access Required</b>\n\n` +
        `You need an invite code to use this bot.\n\n` +
        `Contact your admin for a code, then send:\n` +
        `<code>/join YOUR-CODE</code>`,
      )
      return
    }

    switch (command) {
      case '/setup':        return this.handleSetup(chatId, parts)
      case '/resetkey':     return this.handleResetKey(chatId)
      case '/setalert':     return this.handleSetAlert(chatId, parts)
      case '/listalerts':   return this.handleListAlerts(chatId)
      case '/cancelalert':  return this.handleCancelAlert(chatId, parts)
      case '/cancelalerts': return this.handleCancelAlerts(chatId, parts)
      case '/lotsize':      return this.handleLotSize(chatId, parts)
      case '/price':        return this.handlePrice(chatId, parts)
      case '/pairs':        return this.handlePairs(chatId)
    }
  }

  // ── /start ────────────────────────────────────────────────────

  private async handleStart(chatId: string): Promise<void> {
    const user = await this.usersService.findByChatId(chatId)
    const isAdmin = this.usersService.isAdminChatId(chatId)

    if (isAdmin) {
      await this.sendMessageToUser(
        chatId,
        `👋 <b>Welcome back, Admin!</b>\n\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `<b>Admin Commands</b>\n\n` +
        `<code>/gencode [N]</code> — generate N invite codes\n` +
        `<code>/listcodes</code> — see all codes + status\n` +
        `<code>/revokecode [CODE]</code> — delete a code\n\n` +
        `━━━━━━━━━━━━━━━━━━━━\n` +
        `Type /help for user commands.`,
      )
      return
    }

    if (user?.isApproved && user?.isSetup) {
      await this.sendMessageToUser(
        chatId,
        `👋 <b>Welcome back!</b>\n\nType /help to see all commands.`,
      )
      return
    }

    if (user?.isApproved && !user?.isSetup) {
      await this.sendSetupPrompt(chatId)
      return
    }

    // Not yet approved
    await this.sendMessageToUser(
      chatId,
      `👋 <b>Welcome to Price Alert Bot!</b>\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `This bot is invite-only.\n\n` +
      `Ask your admin for an invite code, then send:\n\n` +
      `<code>/join YOUR-INVITE-CODE</code>`,
    )
  }

  // ── /join ─────────────────────────────────────────────────────

  private async handleJoin(chatId: string, parts: string[]): Promise<void> {
    if (parts.length !== 2) {
      await this.sendMessageToUser(
        chatId,
        `❌ <b>Invalid format</b>\n\n` +
        `Usage: <code>/join YOUR-INVITE-CODE</code>\n\n` +
        `Example: <code>/join TRADE-XK9A2</code>\n\n` +
        `Contact your admin if you don\'t have a code.`,
      )
      return
    }

    const result = await this.inviteService.redeemCode(parts[1], chatId)
    await this.sendMessageToUser(chatId, result.message)
  }

  // ── /gencode (admin only) ─────────────────────────────────────

  private async handleGenCode(chatId: string, parts: string[]): Promise<void> {
    const count = parts.length === 2 ? parseInt(parts[1]) : 1

    if (isNaN(count) || count < 1 || count > 20) {
      await this.sendMessageToUser(
        chatId,
        `❌ Usage: <code>/gencode [1-20]</code>\n\nExample: <code>/gencode 5</code>`,
      )
      return
    }

    const codes = await this.inviteService.generateCodes(count)

    let msg = `✅ <b>${count} Invite Code${count > 1 ? 's' : ''} Generated</b>\n\n`
    msg += `━━━━━━━━━━━━━━━━━━━━\n`
    for (const code of codes) {
      msg += `<code>${code}</code>\n`
    }
    msg += `━━━━━━━━━━━━━━━━━━━━\n`
    msg += `Each code is single-use. Send one to each team member privately.`

    await this.sendMessageToUser(chatId, msg)
  }

  // ── /listcodes (admin only) ───────────────────────────────────

  private async handleListCodes(chatId: string): Promise<void> {
    const msg = await this.inviteService.listCodes()
    await this.sendMessageToUser(chatId, msg)
  }

  // ── /revokecode (admin only) ──────────────────────────────────

  private async handleRevokeCode(chatId: string, parts: string[]): Promise<void> {
    if (parts.length !== 2) {
      await this.sendMessageToUser(
        chatId,
        `❌ Usage: <code>/revokecode [CODE]</code>\n\nExample: <code>/revokecode TRADE-XK9A2</code>`,
      )
      return
    }
    const result = await this.inviteService.revokeCode(parts[1])
    await this.sendMessageToUser(chatId, result.message)
  }

  // ── /setup ────────────────────────────────────────────────────

  private async handleSetup(chatId: string, parts: string[]): Promise<void> {
    if (parts.length !== 2) {
      await this.sendMessageToUser(
        chatId,
        `❌ <b>Invalid format</b>\n\n` +
        `Usage: <code>/setup YOUR_API_KEY</code>\n\n` +
        `Get your free key at: twelvedata.com`,
      )
      return
    }

    const apiKey = parts[1].trim()
    await this.sendMessageToUser(chatId, `🔄 Validating your API key...`)

    const isValid = await this.marketData.validateApiKey(apiKey)

    if (!isValid) {
      await this.sendMessageToUser(
        chatId,
        `❌ <b>Invalid API Key</b>\n\n` +
        `The key you entered didn\'t work.\n\n` +
        `Double-check it at twelvedata.com and try again:\n` +
        `<code>/setup YOUR_API_KEY</code>`,
      )
      return
    }

    await this.usersService.saveApiKey(chatId, apiKey)

    await this.sendMessageToUser(
      chatId,
      `✅ <b>You\'re all set!</b>\n\n` +
      `Your API key has been saved and verified.\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `<b>Try these commands:</b>\n\n` +
      `📊 <code>/setalert GBPUSD SL 1.2700</code>\n` +
      `📦 <code>/lotsize GBPUSD 50 20</code>\n` +
      `💰 <code>/price EURUSD</code>\n` +
      `📋 /help — all commands`,
    )
  }

  // ── /resetkey ─────────────────────────────────────────────────

  private async handleResetKey(chatId: string): Promise<void> {
    await this.usersService.resetUser(chatId)
    await this.sendMessageToUser(
      chatId,
      `🔄 <b>API Key Removed</b>\n\n` +
      `Your Twelve Data API key has been cleared.\n\n` +
      `To set a new one:\n<code>/setup YOUR_NEW_API_KEY</code>`,
    )
  }

  // ── /setalert ─────────────────────────────────────────────────

  private async handleSetAlert(chatId: string, parts: string[]): Promise<void> {
    const user = await this.usersService.findByChatId(chatId)
    if (!user?.isSetup) { await this.sendSetupPrompt(chatId); return }

    if (parts.length !== 4) {
      await this.sendMessageToUser(
        chatId,
        `❌ <b>Invalid format</b>\n\n` +
        `Usage: <code>/setalert [PAIR] [TYPE] [PRICE]</code>\n\n` +
        `Types: <b>SL</b> | <b>TP</b> | <b>TARGET</b>\n\n` +
        `Examples:\n` +
        `<code>/setalert GBPUSD SL 1.3200</code>\n` +
        `<code>/setalert EURUSD TP 1.1500</code>\n` +
        `<code>/setalert XAUUSD TARGET 3300.00</code>`,
      )
      return
    }

    const symbol = parts[1].toUpperCase()
    const type = parts[2].toUpperCase()
    const price = parseFloat(parts[3])

    if (!['SL', 'TP', 'TARGET'].includes(type)) {
      await this.sendMessageToUser(chatId, `❌ Invalid type <b>${type}</b>\n\nValid types: <b>SL</b> | <b>TP</b> | <b>TARGET</b>`)
      return
    }

    const result = await this.priceAlertService.addAlert(chatId, symbol, type as AlertType, price)
    await this.sendMessageToUser(chatId, result.message)
  }

  // ── /listalerts ───────────────────────────────────────────────

  private async handleListAlerts(chatId: string): Promise<void> {
    const user = await this.usersService.findByChatId(chatId)
    if (!user?.isSetup) { await this.sendSetupPrompt(chatId); return }
    const message = await this.priceAlertService.listAlerts(chatId)
    await this.sendMessageToUser(chatId, message)
  }

  // ── /cancelalert ──────────────────────────────────────────────

  private async handleCancelAlert(chatId: string, parts: string[]): Promise<void> {
    if (parts.length !== 2) {
      await this.sendMessageToUser(chatId, `❌ Usage: <code>/cancelalert [ID]</code>\n\nExample: <code>/cancelalert 3</code>`)
      return
    }
    const id = parseInt(parts[1])
    if (isNaN(id)) { await this.sendMessageToUser(chatId, `❌ Invalid ID. Please use a number.`); return }
    const result = await this.priceAlertService.cancelAlert(chatId, id)
    await this.sendMessageToUser(chatId, result.message)
  }

  // ── /cancelalerts ─────────────────────────────────────────────

  private async handleCancelAlerts(chatId: string, parts: string[]): Promise<void> {
    if (parts.length !== 2) {
      await this.sendMessageToUser(
        chatId,
        `❌ Usage: <code>/cancelalerts [SYMBOL or all]</code>\n\n` +
        `<code>/cancelalerts GBPUSD</code>\n` +
        `<code>/cancelalerts all</code>`,
      )
      return
    }
    if (parts[1].toLowerCase() === 'all') {
      const result = await this.priceAlertService.cancelAllAlerts(chatId)
      await this.sendMessageToUser(chatId, result.message)
    } else {
      const result = await this.priceAlertService.cancelAlertsBySymbol(chatId, parts[1])
      await this.sendMessageToUser(chatId, result.message)
    }
  }

  // ── /lotsize ──────────────────────────────────────────────────

  private async handleLotSize(chatId: string, parts: string[]): Promise<void> {
    const user = await this.usersService.findByChatId(chatId)
    if (!user?.isSetup) { await this.sendSetupPrompt(chatId); return }

    if (parts.length !== 4) {
      await this.sendMessageToUser(
        chatId,
        `❌ <b>Invalid format</b>\n\n` +
        `Usage: <code>/lotsize [PAIR] [RISK$] [SL PIPS]</code>\n\n` +
        `Examples:\n` +
        `<code>/lotsize GBPUSD 50 20</code>\n` +
        `<code>/lotsize EURJPY 100 30</code>\n` +
        `<code>/lotsize XAUUSD 200 15</code>`,
      )
      return
    }

    const pair = parts[1]
    const riskUsd = parseFloat(parts[2])
    const slPips = parseFloat(parts[3])

    if (isNaN(riskUsd) || isNaN(slPips)) {
      await this.sendMessageToUser(chatId, `❌ Risk and SL pips must be valid numbers.`)
      return
    }

    await this.sendMessageToUser(chatId, `🔄 Fetching live rate...`)
    const result = await this.lotSizeService.calculate(pair, riskUsd, slPips, user.twelveDataApiKey)
    await this.sendMessageToUser(chatId, result.message)
  }

  // ── /price ────────────────────────────────────────────────────

  private async handlePrice(chatId: string, parts: string[]): Promise<void> {
    const user = await this.usersService.findByChatId(chatId)
    if (!user?.isSetup) { await this.sendSetupPrompt(chatId); return }

    if (parts.length !== 2) {
      await this.sendMessageToUser(chatId, `❌ Usage: <code>/price [PAIR]</code>\n\nExample: <code>/price EURUSD</code>`)
      return
    }

    const symbol = parts[1].toUpperCase()
    const price = await this.marketData.getCurrentPrice(symbol, user.twelveDataApiKey)

    if (price === null) {
      await this.sendMessageToUser(chatId, `❌ Could not fetch price for <b>${symbol}</b>.\n\nCheck the symbol or try again.`)
      return
    }

    await this.sendMessageToUser(chatId, `📊 <b>${symbol}</b>\n\n💰 Price: <b>${price}</b>\n🕐 Live rate`)
  }

  // ── /pairs ────────────────────────────────────────────────────

  private async handlePairs(chatId: string): Promise<void> {
    await this.sendMessageToUser(chatId, this.lotSizeService.getSupportedPairsMessage())
  }

  // ── /help ─────────────────────────────────────────────────────

  private async handleHelp(chatId: string): Promise<void> {
    const isAdmin = this.usersService.isAdminChatId(chatId)

    let msg =
      `🤖 <b>Price Alert Bot — Commands</b>\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `<b>⚙️ Setup</b>\n\n` +
      `<code>/join [CODE]</code> — redeem invite code\n` +
      `<code>/setup [API_KEY]</code> — save Twelve Data key\n` +
      `<code>/resetkey</code> — remove API key\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `<b>🔔 Alerts</b>\n\n` +
      `<code>/setalert [PAIR] [TYPE] [PRICE]</code>\n` +
      `<code>/listalerts</code>\n` +
      `<code>/cancelalert [ID]</code>\n` +
      `<code>/cancelalerts [SYMBOL or all]</code>\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `<b>📦 Lot Size</b>\n\n` +
      `<code>/lotsize [PAIR] [RISK$] [SL PIPS]</code>\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `<b>📊 Market</b>\n\n` +
      `<code>/price [PAIR]</code>\n` +
      `<code>/pairs</code>\n\n` +
      `━━━━━━━━━━━━━━━━━━━━\n` +
      `🔴 SL · 🟢 TP · 🎯 TARGET\n` +
      `Prices checked every 15 minutes ⏱`

    if (isAdmin) {
      msg +=
        `\n\n━━━━━━━━━━━━━━━━━━━━\n` +
        `<b>🔑 Admin</b>\n\n` +
        `<code>/gencode [N]</code> — generate invite codes\n` +
        `<code>/listcodes</code> — view all codes\n` +
        `<code>/revokecode [CODE]</code> — delete a code`
    }

    await this.sendMessageToUser(chatId, msg)
  }

  // ── Helpers ───────────────────────────────────────────────────

  private async sendSetupPrompt(chatId: string): Promise<void> {
    await this.sendMessageToUser(
      chatId,
      `⚠️ <b>API Key Required</b>\n\n` +
      `You need to add your Twelve Data API key first.\n\n` +
      `1️⃣ Get a free key at: <a href="https://twelvedata.com">twelvedata.com</a>\n` +
      `2️⃣ Then send: <code>/setup YOUR_API_KEY</code>`,
    )
  }

  async sendMessageToUser(chatId: string, text: string): Promise<void> {
    try {
      await axios.post(`${this.baseUrl}/sendMessage`, {
        chat_id: chatId,
        text,
        parse_mode: 'HTML',
        disable_web_page_preview: true,
      })
    } catch (error) {
      this.logger.error(`Failed to send to ${chatId}: ${error.message}`)
    }
  }
}
