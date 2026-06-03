import { NestFactory } from '@nestjs/core'
import { AppModule } from './app.module'
import { Logger } from '@nestjs/common'

async function bootstrap() {
  const logger = new Logger('Bootstrap')
  const app = await NestFactory.create(AppModule)

  // Required for Telegram to send JSON webhook payloads
  app.use(require('express').json())

  const port = process.env.PORT ?? 3000
  await app.listen(port, '0.0.0.0')

  const env = process.env.NODE_ENV ?? 'development'
  logger.log(`Price Alert Bot running on port ${port} [${env}]`)

  if (env === 'production') {
    logger.log(`Webhook endpoint: ${process.env.WEBHOOK_URL}/webhook/<token>`)
  } else {
    logger.log('Using polling mode for local development')
  }
}

bootstrap()
