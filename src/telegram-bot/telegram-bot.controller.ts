import { Controller, Post, Param, Body, HttpCode } from '@nestjs/common'
import { ConfigService } from '@nestjs/config'
import { TelegramBotService } from './telegram-bot.service'

@Controller('webhook')
export class TelegramBotController {
  private readonly botToken: string

  constructor(
    private readonly telegramBotService: TelegramBotService,
    private readonly configService: ConfigService,
  ) {
    this.botToken = this.configService.getOrThrow('TELEGRAM_BOT_TOKEN')
  }

  // Telegram sends POST requests to /webhook/<bot-token>
  // The bot token in the URL acts as a secret — only Telegram knows it
  @Post(':token')
  @HttpCode(200)
  async handleWebhook(
    @Param('token') token: string,
    @Body() update: any,
  ): Promise<{ ok: boolean }> {
    // Reject requests with wrong token
    if (token !== this.botToken) {
      return { ok: false }
    }

    // Process the update asynchronously — return 200 immediately
    // Telegram will retry if it doesn't get 200 within 60s
    this.telegramBotService.handleUpdate(update).catch(() => {})

    return { ok: true }
  }
}
