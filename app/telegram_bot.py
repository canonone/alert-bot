import logging
from typing import Any

from telegram import Bot, Update
from telegram.ext import ContextTypes

from app.services.invite_service import InviteService
from app.services.market_data_service import MarketDataService
from app.services.price_alert_service import PriceAlertService
from app.services.users_service import UsersService

logger = logging.getLogger("TelegramBotService")


class TelegramBotService:
    def __init__(
        self,
        users_service: UsersService,
        price_alert_service: PriceAlertService,
        market_data: MarketDataService,
        invite_service: InviteService,
    ) -> None:
        self.users_service = users_service
        self.price_alert_service = price_alert_service
        self.market_data = market_data
        self.invite_service = invite_service

        # Wired up after construction in main.py (avoids circular imports)
        self.bot: Bot | None = None
        self.finnhub_price_service: Any = None
        self.retracement_zone_service: Any = None

    # ── PTB entrypoint ────────────────────────────────────────────

    async def handle_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message or not message.text:
            return

        chat_id = str(message.chat.id)
        first_name = message.from_user.first_name if message.from_user else None
        username = message.from_user.username if message.from_user else None
        text = message.text.strip()

        await self.users_service.find_or_create(chat_id, first_name, username)
        await self._handle_command(chat_id, text)

    # ── Command router ────────────────────────────────────────────

    async def _handle_command(self, chat_id: str, text: str) -> None:
        parts = [p for p in text.split(" ") if p]
        if not parts:
            return
        command = parts[0].lower()

        # These commands work without approval
        if command == "/start":
            await self._handle_start(chat_id)
            return
        if command == "/join":
            await self._handle_join(chat_id, parts)
            return
        if command == "/help":
            await self._handle_help(chat_id)
            return

        # Admin-only commands
        if self.users_service.is_admin_chat_id(chat_id):
            if command == "/gencode":
                await self._handle_gencode(chat_id, parts)
                return
            if command == "/listcodes":
                await self._handle_listcodes(chat_id)
                return
            if command == "/revokecode":
                await self._handle_revokecode(chat_id, parts)
                return

        # All other commands require approval first
        user = await self.users_service.find_by_chat_id(chat_id)
        if not user or not user.is_approved:
            await self.send_message_to_user(
                chat_id,
                "🔒 <b>Access Required</b>\n\n"
                "You need an invite code to use this bot.\n\n"
                "Contact your admin for a code, then send:\n"
                "<code>/join YOUR-CODE</code>",
            )
            return

        if command == "/setup":
            await self._handle_setup(chat_id, parts)
        elif command == "/resetkey":
            await self._handle_resetkey(chat_id)
        elif command == "/setalert":
            await self._handle_setalert(chat_id, text)
        elif command == "/listalerts":
            await self._handle_listalerts(chat_id)
        elif command == "/cancelalert":
            await self._handle_cancelalert(chat_id, parts)
        elif command == "/cancelalerts":
            await self._handle_cancelalerts(chat_id, parts)
        elif command == "/price":
            await self._handle_price(chat_id, parts)
        elif command == "/pairs":
            await self._handle_pairs(chat_id)
        elif command == "/xauzone":
            await self._handle_xauzone(chat_id, parts)
        elif command == "/zonealerts":
            await self._handle_zonealerts(chat_id, parts)

    # ── /start ────────────────────────────────────────────────────

    async def _handle_start(self, chat_id: str) -> None:
        user = await self.users_service.find_by_chat_id(chat_id)
        is_admin = self.users_service.is_admin_chat_id(chat_id)

        if is_admin:
            await self.send_message_to_user(
                chat_id,
                "👋 <b>Welcome back, Admin!</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<b>Admin Commands</b>\n\n"
                "<code>/gencode [N]</code> — generate N invite codes\n"
                "<code>/listcodes</code> — see all codes + status\n"
                "<code>/revokecode [CODE]</code> — delete a code\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Type /help for user commands.",
            )
            return

        if user and user.is_approved and user.is_setup:
            await self.send_message_to_user(chat_id, "👋 <b>Welcome back!</b>\n\nType /help to see all commands.")
            return

        if user and user.is_approved and not user.is_setup:
            await self._send_setup_prompt(chat_id)
            return

        # Not yet approved
        await self.send_message_to_user(
            chat_id,
            "👋 <b>Welcome to Price Alert Bot!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "This bot is invite-only.\n\n"
            "Ask your admin for an invite code, then send:\n\n"
            "<code>/join YOUR-INVITE-CODE</code>",
        )

    # ── /join ─────────────────────────────────────────────────────

    async def _handle_join(self, chat_id: str, parts: list[str]) -> None:
        if len(parts) != 2:
            await self.send_message_to_user(
                chat_id,
                "❌ <b>Invalid format</b>\n\n"
                "Usage: <code>/join YOUR-INVITE-CODE</code>\n\n"
                "Example: <code>/join TRADE-XK9A2</code>\n\n"
                "Contact your admin if you don't have a code.",
            )
            return

        _, message = await self.invite_service.redeem_code(parts[1], chat_id)
        await self.send_message_to_user(chat_id, message)

    # ── /gencode (admin only) ─────────────────────────────────────

    async def _handle_gencode(self, chat_id: str, parts: list[str]) -> None:
        count = None
        if len(parts) == 2:
            try:
                count = int(parts[1])
            except ValueError:
                count = None
        else:
            count = 1

        if count is None or count < 1 or count > 20:
            await self.send_message_to_user(
                chat_id, "❌ Usage: <code>/gencode [1-20]</code>\n\nExample: <code>/gencode 5</code>"
            )
            return

        codes = await self.invite_service.generate_codes(count)

        msg = f"✅ <b>{count} Invite Code{'s' if count > 1 else ''} Generated</b>\n\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n"
        for code in codes:
            msg += f"<code>{code}</code>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n"
        msg += "Each code is single-use. Send one to each team member privately."

        await self.send_message_to_user(chat_id, msg)

    # ── /listcodes (admin only) ───────────────────────────────────

    async def _handle_listcodes(self, chat_id: str) -> None:
        msg = await self.invite_service.list_codes()
        await self.send_message_to_user(chat_id, msg)

    # ── /revokecode (admin only) ──────────────────────────────────

    async def _handle_revokecode(self, chat_id: str, parts: list[str]) -> None:
        if len(parts) != 2:
            await self.send_message_to_user(
                chat_id, "❌ Usage: <code>/revokecode [CODE]</code>\n\nExample: <code>/revokecode TRADE-XK9A2</code>"
            )
            return
        _, message = await self.invite_service.revoke_code(parts[1])
        await self.send_message_to_user(chat_id, message)

    # ── /setup ────────────────────────────────────────────────────

    async def _handle_setup(self, chat_id: str, parts: list[str]) -> None:
        if len(parts) != 2:
            await self.send_message_to_user(
                chat_id,
                "❌ <b>Invalid format</b>\n\n"
                "Usage: <code>/setup YOUR_API_KEY</code>\n\n"
                "Get your free key at: twelvedata.com",
            )
            return

        api_key = parts[1].strip()
        await self.send_message_to_user(chat_id, "🔄 Validating your API key...")

        is_valid = await self.market_data.validate_api_key(api_key)

        if not is_valid:
            await self.send_message_to_user(
                chat_id,
                "❌ <b>Invalid API Key</b>\n\n"
                "The key you entered didn't work.\n\n"
                "Double-check it at twelvedata.com and try again:\n"
                "<code>/setup YOUR_API_KEY</code>",
            )
            return

        await self.users_service.save_api_key(chat_id, api_key)

        await self.send_message_to_user(
            chat_id,
            "✅ <b>You're all set!</b>\n\n"
            "Your API key has been saved and verified.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Try these commands:</b>\n\n"
            "📊 <code>/setalert GBPUSD SL 1.2700</code>\n"
            "💰 <code>/price EURUSD</code>\n"
            "📋 /help — all commands",
        )

    # ── /resetkey ─────────────────────────────────────────────────

    async def _handle_resetkey(self, chat_id: str) -> None:
        await self.users_service.reset_user(chat_id)
        await self.send_message_to_user(
            chat_id,
            "🔄 <b>API Key Removed</b>\n\n"
            "Your Twelve Data API key has been cleared.\n\n"
            "To set a new one:\n<code>/setup YOUR_NEW_API_KEY</code>",
        )

    # ── /setalert ─────────────────────────────────────────────────

    async def _handle_setalert(self, chat_id: str, text: str) -> None:
        user = await self.users_service.find_by_chat_id(chat_id)
        if not user or not user.is_setup:
            await self._send_setup_prompt(chat_id)
            return

        command_part, _, note_part = text.partition("|")
        note = note_part.strip() or None
        parts = [p for p in command_part.split(" ") if p]

        if len(parts) not in (4, 5):
            await self.send_message_to_user(
                chat_id,
                "❌ <b>Invalid format</b>\n\n"
                "Usage: <code>/setalert [PAIR] [TYPE] [PRICE]</code>\n"
                "For TARGET, an optional invalidation price:\n"
                "<code>/setalert [PAIR] TARGET [PRICE] [INVALIDATION_PRICE]</code>\n\n"
                "Add an optional note with <code>|</code>:\n"
                "<code>/setalert [PAIR] [TYPE] [PRICE] | [NOTE]</code>\n\n"
                "Types: <b>SL</b> | <b>TP</b> | <b>TARGET</b>\n\n"
                "Examples:\n"
                "<code>/setalert GBPUSD SL 1.3200</code>\n"
                "<code>/setalert EURUSD TP 1.1500</code>\n"
                "<code>/setalert XAUUSD TARGET 3300.00</code>\n"
                "<code>/setalert XAUUSD TARGET 3300.00 3250.00</code>\n"
                "<code>/setalert EURUSD SL 1.0800 | protect the long</code>",
            )
            return

        symbol = parts[1].upper()
        type_ = parts[2].upper()
        try:
            price = float(parts[3])
        except ValueError:
            price = float("nan")

        if type_ not in ("SL", "TP", "TARGET"):
            await self.send_message_to_user(
                chat_id, f"❌ Invalid type <b>{type_}</b>\n\nValid types: <b>SL</b> | <b>TP</b> | <b>TARGET</b>"
            )
            return

        if len(parts) == 5 and type_ != "TARGET":
            await self.send_message_to_user(
                chat_id,
                "❌ An invalidation price is only valid for <b>TARGET</b> alerts.\n\n"
                "Usage: <code>/setalert [PAIR] [TYPE] [PRICE]</code>",
            )
            return

        invalidation_price: float | None = None
        if len(parts) == 5:
            try:
                invalidation_price = float(parts[4])
            except ValueError:
                invalidation_price = float("nan")

        success, message = await self.price_alert_service.add_alert(
            chat_id, symbol, type_, price, invalidation_price, note
        )
        await self.send_message_to_user(chat_id, message)

        if success and self.finnhub_price_service is not None:
            await self.finnhub_price_service.subscribe_to_symbol(symbol)

    # ── /listalerts ───────────────────────────────────────────────

    async def _handle_listalerts(self, chat_id: str) -> None:
        user = await self.users_service.find_by_chat_id(chat_id)
        if not user or not user.is_setup:
            await self._send_setup_prompt(chat_id)
            return
        message = await self.price_alert_service.list_alerts(chat_id)
        await self.send_message_to_user(chat_id, message)

    # ── /cancelalert ──────────────────────────────────────────────

    async def _handle_cancelalert(self, chat_id: str, parts: list[str]) -> None:
        if len(parts) != 2:
            await self.send_message_to_user(
                chat_id, "❌ Usage: <code>/cancelalert [ID]</code>\n\nExample: <code>/cancelalert 3</code>"
            )
            return
        try:
            alert_id = int(parts[1])
        except ValueError:
            await self.send_message_to_user(chat_id, "❌ Invalid ID. Please use a number.")
            return
        _, message = await self.price_alert_service.cancel_alert(chat_id, alert_id)
        await self.send_message_to_user(chat_id, message)

    # ── /cancelalerts ─────────────────────────────────────────────

    async def _handle_cancelalerts(self, chat_id: str, parts: list[str]) -> None:
        if len(parts) != 2:
            await self.send_message_to_user(
                chat_id,
                "❌ Usage: <code>/cancelalerts [SYMBOL or all]</code>\n\n"
                "<code>/cancelalerts GBPUSD</code>\n"
                "<code>/cancelalerts all</code>",
            )
            return
        if parts[1].lower() == "all":
            _, message = await self.price_alert_service.cancel_all_alerts(chat_id)
        else:
            _, message = await self.price_alert_service.cancel_alerts_by_symbol(chat_id, parts[1])
        await self.send_message_to_user(chat_id, message)

    # ── /price ────────────────────────────────────────────────────

    async def _handle_price(self, chat_id: str, parts: list[str]) -> None:
        user = await self.users_service.find_by_chat_id(chat_id)
        if not user or not user.is_setup:
            await self._send_setup_prompt(chat_id)
            return

        if len(parts) != 2:
            await self.send_message_to_user(
                chat_id, "❌ Usage: <code>/price [PAIR]</code>\n\nExample: <code>/price EURUSD</code>"
            )
            return

        symbol = parts[1].upper()
        price, _ = await self.market_data.get_current_price(symbol, user.twelve_data_api_key)

        if price is None:
            await self.send_message_to_user(chat_id, f"❌ Could not fetch price for <b>{symbol}</b>.\n\nCheck the symbol or try again.")
            return

        await self.send_message_to_user(chat_id, f"📊 <b>{symbol}</b>\n\n💰 Price: <b>{price}</b>\n🕐 Live rate")

    # ── /pairs ────────────────────────────────────────────────────

    async def _handle_pairs(self, chat_id: str) -> None:
        await self.send_message_to_user(chat_id, self.price_alert_service.get_supported_pairs_message())

    # ── /xauzone ──────────────────────────────────────────────────

    async def _handle_xauzone(self, chat_id: str, parts: list[str]) -> None:
        if self.retracement_zone_service is None:
            await self.send_message_to_user(chat_id, "❌ The 4H zone feature is not configured on this server.")
            return
        pair = parts[1].upper() if len(parts) >= 2 else "XAUUSD"
        await self.send_message_to_user(chat_id, self.retracement_zone_service.get_status_message(pair))

    # ── /zonealerts ───────────────────────────────────────────────

    async def _handle_zonealerts(self, chat_id: str, parts: list[str]) -> None:
        if self.retracement_zone_service is None:
            await self.send_message_to_user(chat_id, "❌ The 4H zone feature is not configured on this server.")
            return
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            await self.send_message_to_user(
                chat_id, "❌ Usage: <code>/zonealerts on</code> or <code>/zonealerts off</code>"
            )
            return
        enabled = parts[1].lower() == "on"
        await self.users_service.set_zone_alerts_enabled(chat_id, enabled)
        status = "enabled ✅" if enabled else "disabled ❌"
        await self.send_message_to_user(chat_id, f"4H zone alerts {status}.")

    # ── /help ─────────────────────────────────────────────────────

    async def _handle_help(self, chat_id: str) -> None:
        is_admin = self.users_service.is_admin_chat_id(chat_id)

        msg = (
            "🤖 <b>Price Alert Bot — Commands</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚙️ Setup</b>\n\n"
            "<code>/join [CODE]</code> — redeem invite code\n"
            "<code>/setup [API_KEY]</code> — save Twelve Data key\n"
            "<code>/resetkey</code> — remove API key\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>🔔 Alerts</b>\n\n"
            "<code>/setalert [PAIR] [TYPE] [PRICE]</code>\n"
            "<code>/setalert [PAIR] TARGET [PRICE] [INVALIDATION]</code>\n"
            "<code>/listalerts</code>\n"
            "<code>/cancelalert [ID]</code>\n"
            "<code>/cancelalerts [SYMBOL or all]</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📊 Market</b>\n\n"
            "<code>/price [PAIR]</code>\n"
            "<code>/pairs</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>🟡 4H Zones</b>\n\n"
            "<code>/xauzone [PAIR]</code> — current zone status (default XAUUSD)\n"
            "<code>/zonealerts [on|off]</code> — zone alert notifications\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔴 SL · 🟢 TP · 🎯 TARGET\n"
            "Real-time price alerts via live feed ⚡"
        )

        if is_admin:
            msg += (
                "\n\n━━━━━━━━━━━━━━━━━━━━\n"
                "<b>🔑 Admin</b>\n\n"
                "<code>/gencode [N]</code> — generate invite codes\n"
                "<code>/listcodes</code> — view all codes\n"
                "<code>/revokecode [CODE]</code> — delete a code"
            )

        await self.send_message_to_user(chat_id, msg)

    # ── Helpers ───────────────────────────────────────────────────

    async def _send_setup_prompt(self, chat_id: str) -> None:
        await self.send_message_to_user(
            chat_id,
            "⚠️ <b>API Key Required</b>\n\n"
            "You need to add your Twelve Data API key first.\n\n"
            '1️⃣ Get a free key at: <a href="https://twelvedata.com">twelvedata.com</a>\n'
            "2️⃣ Then send: <code>/setup YOUR_API_KEY</code>",
        )

    async def send_message_to_user(self, chat_id: str, text: str) -> None:
        if self.bot is None:
            logger.error(f"Cannot send to {chat_id}: bot not initialized")
            return
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as error:
            logger.error(f"Failed to send to {chat_id}: {error}")
