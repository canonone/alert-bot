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
