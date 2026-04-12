"""
Database connection and session management.
Async SQLAlchemy with proper lifecycle handling.
"""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.logging import logger

Base = declarative_base()

def get_database_url() -> str:
    """Get the database connection URL.
    
    Handles various DATABASE_URL formats from cloud providers
    (Railway, Supabase, Heroku) and converts them to asyncpg format.
    """
    db_url = settings.DATABASE_URL
    if db_url:
        # Convert postgres:// or postgresql:// to postgresql+asyncpg://
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif not db_url.startswith("postgresql+asyncpg://"):
            db_url = f"postgresql+asyncpg://{db_url}"
        return db_url
    
    postgres_user = settings.POSTGRES_USER
    postgres_password = settings.POSTGRES_PASSWORD
    postgres_host = settings.POSTGRES_HOST
    postgres_port = settings.POSTGRES_PORT
    postgres_db = settings.POSTGRES_DB

    if settings.ENVIRONMENT == "development" and postgres_host == "postgres":
        postgres_host = "localhost"
    
    if not postgres_user or not postgres_password:
        raise ValueError(
            "Database credentials not configured. "
            "Set DATABASE_URL or POSTGRES_USER + POSTGRES_PASSWORD env vars."
        )
    
    return f"postgresql+asyncpg://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/{postgres_db}"

_engine_kwargs = {
    "echo": settings.DEBUG,
    "future": True,
    "pool_pre_ping": True,
}

# Disable SQLAlchemy's connection pooling to prevent conflicts with 
# Supavisor (Supabase's session-mode connection pooler)
_engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(get_database_url(), **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for getting database sessions.
    Routes manage their own commits — this dependency only handles
    rollback on error and cleanup.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.error(f"Database session error: {e}")
            raise
        finally:
            await session.close()

async def init_db() -> None:
    """Initialize database tables.
    
    Non-fatal: if the database is unreachable the app still starts
    in a degraded state so the health-check endpoint can report the
    issue instead of crashing the whole process.

    IMPORTANT: All ORM models must be imported before create_all is called
    so that SQLAlchemy's metadata registry contains their table definitions.
    Failure to import a model means its table is silently skipped.
    """
    try:
        # Register all ORM-mapped tables with Base.metadata before create_all.
        # Models defined outside app/models/models.py (e.g. EventOutbox in
        # outbox.py) must be explicitly imported here.
        from app.models import models as _models          # noqa: F401  — registers all app tables
        from app.core.outbox import EventOutbox           # noqa: F401  — registers event_outbox
        _ = _models, EventOutbox                          # suppress "unused import" warnings

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Database initialization failed (app will start degraded): {e}")
        logger.error(
            "Hint: Set DATABASE_URL to your Supabase *direct* connection string "
            "(Session mode, port 5432). Pooler / Transaction mode often causes "
            "'Tenant or user not found' errors with asyncpg."
        )

async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()
    logger.info("Database connections closed")
