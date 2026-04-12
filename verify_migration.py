"""verify_migration.py — confirms event_outbox and indexes exist."""
import asyncio
import sys


async def main():
    from app.core.database import get_database_url
    import asyncpg

    url = get_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(url)

    tables = await conn.fetch(
        "SELECT tablename FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename = 'event_outbox'"
    )
    print("event_outbox exists:", len(tables) > 0)

    indexes = await conn.fetch(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' "
        "AND indexname IN ('idx_outbox_status_created', 'uq_agent_output_success', 'idx_agent_output_session_type_status')"
    )
    for r in indexes:
        print("index OK:", r["indexname"])

    count = await conn.fetchval("SELECT COUNT(*) FROM event_outbox")
    print("event_outbox row count:", count)

    await conn.close()
    print("Verification complete.")


if __name__ == "__main__":
    asyncio.run(main())
