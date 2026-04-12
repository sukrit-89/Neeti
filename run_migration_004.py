"""
run_migration_004.py — applies migrations/004_production_hardening.sql
Run from the project root with the venv active:

    python run_migration_004.py

Safe to run multiple times (all DDL uses IF NOT EXISTS).
"""
import asyncio
import sys
import pathlib


async def main():
    # Bootstrap settings so get_database_url() can read the .env
    try:
        from app.core.database import get_database_url
    except Exception as e:
        print(f"[ERROR] Could not import app: {e}", file=sys.stderr)
        print("       Make sure you are running from the project root with the venv active.", file=sys.stderr)
        sys.exit(1)

    sql_path = pathlib.Path("migrations/004_production_hardening.sql")
    if not sql_path.exists():
        print(f"[ERROR] Migration file not found: {sql_path}", file=sys.stderr)
        sys.exit(1)

    sql = sql_path.read_text(encoding="utf-8")

    # Build a synchronous psycopg2 / asyncpg URL.
    # get_database_url() returns postgresql+asyncpg://... — strip the driver prefix.
    raw_url = get_database_url()
    # Convert to psycopg2-compatible URL for the migration runner (plain asyncpg)
    asyncpg_url = raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    try:
        import asyncpg
    except ImportError:
        print("[ERROR] asyncpg not installed.  Run:  pip install asyncpg", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO]  Connecting to database...")
    try:
        conn = await asyncpg.connect(asyncpg_url)
    except Exception as e:
        print(f"[ERROR] Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO]  Running {sql_path} ...")
    try:
        await conn.execute(sql)
        print("[OK]    Migration 004 applied successfully.")
        print()
        print("Tables created / verified:")
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename IN ('event_outbox', 'agent_outputs')"
        )
        for r in rows:
            print(f"  ✓ {r['tablename']}")

        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
            "AND indexname IN ('idx_outbox_status_created','uq_agent_output_success','idx_agent_output_session_type_status')"
        )
        for r in indexes:
            print(f"  ✓ index: {r['indexname']}")

    except Exception as e:
        print(f"[ERROR] Migration failed: {e}", file=sys.stderr)
        await conn.close()
        sys.exit(1)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
