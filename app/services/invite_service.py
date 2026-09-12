import datetime
import logging
import random

from sqlalchemy import delete, select, update

from app.db import SessionLocal
from app.models import InviteCode, User

logger = logging.getLogger("InviteService")

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion


class InviteService:
    # ── Generate N invite codes ───────────────────────────────────

    async def generate_codes(self, count: int) -> list[str]:
        codes: list[str] = []

        async with SessionLocal() as session:
            for _ in range(count):
                code = self._make_code()
                session.add(InviteCode(code=code, used=False))
                codes.append(code)
            await session.commit()

        logger.info(f"Generated {count} invite code(s)")
        return codes

    # ── Redeem a code ─────────────────────────────────────────────

    async def redeem_code(self, code: str, chat_id: str) -> tuple[bool, str]:
        upper = code.upper().strip()

        async with SessionLocal() as session:
            # Check if user is already approved
            user = await session.get(User, chat_id)
            if user and user.is_approved:
                return True, "✅ You already have access. Run /setup to continue."

            result = await session.execute(select(InviteCode).where(InviteCode.code == upper))
            invite_code = result.scalar_one_or_none()

            if invite_code is None:
                return False, (
                    "❌ <b>Invalid invite code.</b>\n\n"
                    "Double-check the code and try again, or contact your admin for a new one."
                )

            if invite_code.used:
                return False, (
                    "❌ <b>This invite code has already been used.</b>\n\n"
                    "Each code is single-use. Contact your admin for a new one."
                )

            # Mark code as used
            invite_code.used = True
            invite_code.used_by = chat_id
            invite_code.used_at = datetime.datetime.now(datetime.timezone.utc)

            # Approve the user
            await session.execute(update(User).where(User.chat_id == chat_id).values(is_approved=True))
            await session.commit()

        logger.info(f"Code {upper} redeemed by {chat_id}")

        return True, (
            "✅ <b>Access Granted!</b>\n\n"
            "Welcome to the team. You're now verified.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Next step — add your Twelve Data API key:\n\n"
            '1️⃣ Get a free key at: <a href="https://twelvedata.com">twelvedata.com</a>\n'
            "2️⃣ Send: <code>/setup YOUR_API_KEY</code>"
        )

    # ── List all codes (for admin) ────────────────────────────────

    async def list_codes(self) -> str:
        async with SessionLocal() as session:
            result = await session.execute(select(InviteCode).order_by(InviteCode.created_at.desc()))
            codes = list(result.scalars().all())

        if not codes:
            return "📭 No invite codes generated yet.\n\nUse <code>/gencode 5</code> to generate some."

        unused = [c for c in codes if not c.used]
        used = [c for c in codes if c.used]

        msg = "📋 <b>Invite Codes</b>\n\n"

        if unused:
            msg += f"✅ <b>Available ({len(unused)})</b>\n"
            for c in unused:
                msg += f"  <code>{c.code}</code>\n"
            msg += "\n"

        if used:
            msg += f"🔒 <b>Used ({len(used)})</b>\n"
            for c in used:
                msg += f"  <code>{c.code}</code> — by {c.used_by}\n"

        return msg

    # ── Revoke a code (admin) ─────────────────────────────────────

    async def revoke_code(self, code: str) -> tuple[bool, str]:
        upper = code.upper().strip()

        async with SessionLocal() as session:
            result = await session.execute(delete(InviteCode).where(InviteCode.code == upper))
            await session.commit()
            affected = result.rowcount or 0

        if affected == 0:
            return False, f"❌ Code <code>{upper}</code> not found."

        return True, f"✅ Code <code>{upper}</code> deleted."

    # ── Code generator ────────────────────────────────────────────
    # Format: TRADE-XXXXX (5 random alphanumeric chars)

    def _make_code(self) -> str:
        suffix = "".join(random.choice(CODE_CHARS) for _ in range(5))
        return f"TRADE-{suffix}"
