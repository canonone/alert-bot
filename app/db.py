from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app import config

DATABASE_URL = (
    f"postgresql+asyncpg://{config.DB_USERNAME}:{config.DB_PASSWORD}"
    f"@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}"
)

engine = create_async_engine(DATABASE_URL, echo=config.NODE_ENV == "development")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    # Mirrors TypeORM's `synchronize: true` — create tables on startup, no migrations.
    from app import models  # noqa: F401  (ensure models are registered on Base)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all never alters an existing table, so add columns introduced after a deployment's
        # first run. Additive only and idempotent — nothing is ever dropped at runtime.
        for statement in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_sweep_alerts_enabled BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_continuation_alerts_enabled "
            "BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE daily_sweep_setups ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'SWEEP'",
            "ALTER TABLE daily_sweep_setups ADD COLUMN IF NOT EXISTS level2 NUMERIC(18, 6)",
            "ALTER TABLE daily_sweep_setups ADD COLUMN IF NOT EXISTS stage INTEGER NOT NULL DEFAULT 0",
        ):
            await conn.execute(text(statement))
