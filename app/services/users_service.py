import logging

from sqlalchemy import select, update

from app import config
from app.db import SessionLocal
from app.models import User

logger = logging.getLogger("UsersService")


class UsersService:
    def __init__(self) -> None:
        self.admin_chat_id = config.ADMIN_CHAT_ID

    # ── Find or create user on first contact ─────────────────────

    async def find_or_create(
        self, chat_id: str, first_name: str | None = None, username: str | None = None
    ) -> User:
        async with SessionLocal() as session:
            user = await session.get(User, chat_id)
            is_admin = chat_id == self.admin_chat_id

            if user is None:
                user = User(
                    chat_id=chat_id,
                    first_name=first_name,
                    username=username,
                    is_approved=is_admin,  # admin is auto-approved
                    is_admin=is_admin,
                    is_setup=False,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
                logger.info(f"New user: {chat_id} (@{username or 'unknown'}) admin={is_admin}")
            elif is_admin and not user.is_admin:
                # Retroactively mark as admin if ADMIN_CHAT_ID was set after first contact
                await session.execute(
                    update(User).where(User.chat_id == chat_id).values(is_admin=True, is_approved=True)
                )
                await session.commit()
                user.is_admin = True
                user.is_approved = True

            return user

    # ── Save API key ──────────────────────────────────────────────

    async def save_api_key(self, chat_id: str, api_key: str) -> User | None:
        async with SessionLocal() as session:
            await session.execute(
                update(User).where(User.chat_id == chat_id).values(twelve_data_api_key=api_key, is_setup=True)
            )
            await session.commit()
            return await session.get(User, chat_id)

    # ── Look up a user ────────────────────────────────────────────

    async def find_by_chat_id(self, chat_id: str) -> User | None:
        async with SessionLocal() as session:
            return await session.get(User, chat_id)

    # ── Get all setup users (for cron) ────────────────────────────

    async def find_all_setup_users(self) -> list[User]:
        async with SessionLocal() as session:
            result = await session.execute(
                select(User).where(User.is_setup.is_(True), User.is_approved.is_(True))
            )
            return list(result.scalars().all())

    # ── Reset API key ─────────────────────────────────────────────

    async def reset_user(self, chat_id: str) -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(User).where(User.chat_id == chat_id).values(twelve_data_api_key=None, is_setup=False)
            )
            await session.commit()

    # ── Per-user alert ID counter ─────────────────────────────────

    async def get_next_alert_id(self, chat_id: str) -> int:
        async with SessionLocal() as session:
            await session.execute(
                update(User).where(User.chat_id == chat_id).values(alert_id_counter=User.alert_id_counter + 1)
            )
            await session.commit()
            user = await session.get(User, chat_id)
            return user.alert_id_counter

    async def reset_all_alert_id_counters(self) -> None:
        async with SessionLocal() as session:
            await session.execute(update(User).values(alert_id_counter=0))
            await session.commit()

    # ── Check admin ───────────────────────────────────────────────

    def is_admin_chat_id(self, chat_id: str) -> bool:
        return chat_id == self.admin_chat_id
