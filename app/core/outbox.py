"""
Transactional Outbox — reliable event delivery for agent completions.

Pattern:
  1. The outbox row is written in the SAME DB transaction as the agent_output
     INSERT (via _persist_and_publish_with_outbox in agent_tasks.py).
  2. This sweeper runs periodically (every SWEEP_INTERVAL_SEC seconds) and
     publishes any pending/failed rows to Redis, then marks them published.
  3. Because Redis publish is idempotent from a business perspective (consumers
     use output_id and agent_type to deduplicate), retrying is always safe.

Usage — start alongside your Celery worker or as a standalone process:

    python -m app.core.outbox

Or from your app startup / background task runner:

    asyncio.create_task(run_outbox_sweeper())
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select, update, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.redis import redis_client
from app.core.logging import logger

# ── Configuration ──────────────────────────────────────────────────────────────
SWEEP_INTERVAL_SEC = 10       # how often to poll for un-published events
MAX_ATTEMPTS       = 5        # give up after this many failures per row
BATCH_SIZE         = 50       # rows per sweep

# ── ORM-lite model (avoids circular imports with models.py) ────────────────────
from sqlalchemy import Column, Integer, SmallInteger, String, Text, DateTime
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from app.core.database import Base


class EventOutbox(Base):
    """
    Transactional outbox table.
    Written inside the same transaction as the agent_output row.
    Read and cleared by the sweeper below.
    """
    __tablename__ = "event_outbox"

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id   = Column(Integer,    nullable=False)
    event_type   = Column(String(100),nullable=False)
    payload      = Column(JSONB,      nullable=False, default=dict)
    status       = Column(String(20), nullable=False, default="pending")  # pending|published|failed
    attempts     = Column(SmallInteger, nullable=False, default=0)
    last_error   = Column(Text,       nullable=True)
    created_at   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    published_at = Column(DateTime(timezone=True), nullable=True)


# ── Sweeper ────────────────────────────────────────────────────────────────────

async def _publish_batch(db: AsyncSession) -> int:
    """
    Fetch up to BATCH_SIZE pending/failed rows, attempt Redis publish,
    and mark them published or failed (if exhausted).

    Returns the number of rows processed.
    """
    result = await db.execute(
        select(EventOutbox)
        .where(
            EventOutbox.status.in_(["pending", "failed"]),
            EventOutbox.attempts < MAX_ATTEMPTS,
        )
        .order_by(EventOutbox.created_at)
        .limit(BATCH_SIZE)
        # Prevent other sweeper instances from picking up the same rows
        .with_for_update(skip_locked=True)
    )
    rows: list[EventOutbox] = list(result.scalars().all())

    if not rows:
        return 0

    published, failed = 0, 0
    for row in rows:
        row.attempts += 1
        try:
            if redis_client.client is None:
                raise RuntimeError("Redis client not connected")

            # BUG-14 FIX: After the RT-01 session-isolation fix, WebSocket
            # handlers subscribe ONLY to session-scoped channels:
            #   events:session:{id}:{type}
            # The previous code only published to the global channel
            #   events:{type}
            # so no WS subscriber ever received outbox-delivered events.
            # Publish to BOTH to maintain backward compat (global) and
            # ensure session-scoped WS subscribers receive the event.
            global_channel  = f"events:{row.event_type}"
            session_channel = f"events:session:{row.session_id}:{row.event_type}"
            payload_str = json.dumps(row.payload)

            async with redis_client.client.pipeline(transaction=False) as pipe:
                pipe.publish(global_channel,  payload_str)
                pipe.publish(session_channel, payload_str)
                await pipe.execute()

            row.status       = "published"
            row.published_at = datetime.now(timezone.utc)
            row.last_error   = None
            published += 1
        except Exception as exc:
            row.last_error = str(exc)
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
                logger.error(
                    "outbox: gave up publishing event id=%s session=%s type=%s "
                    "after %d attempts. last_error=%s",
                    row.id, row.session_id, row.event_type, row.attempts, exc,
                )
            else:
                row.status = "failed"  # will be retried on next sweep
                logger.warning(
                    "outbox: publish failed for id=%s (attempt %d/%d): %s",
                    row.id, row.attempts, MAX_ATTEMPTS, exc,
                )
            failed += 1

    await db.commit()
    if published or failed:
        logger.info(
            "outbox sweep: published=%d, retryable_failed=%d, exhausted=%d",
            published,
            sum(1 for r in rows if r.status == "failed" and r.attempts < MAX_ATTEMPTS),
            sum(1 for r in rows if r.status == "failed" and r.attempts >= MAX_ATTEMPTS),
        )
    return len(rows)


async def run_outbox_sweeper() -> None:
    """
    Infinite loop that sweeps the outbox table every SWEEP_INTERVAL_SEC seconds.
    Designed to run as an asyncio background task.
    Errors are caught and logged — the sweeper never crashes.
    """
    logger.info("outbox sweeper started (interval=%ds)", SWEEP_INTERVAL_SEC)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await _publish_batch(db)
        except Exception as exc:
            logger.error("outbox sweeper unexpected error: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_SEC)


# ── Entrypoint for standalone execution ───────────────────────────────────────
if __name__ == "__main__":
    from app.core.redis import redis_client as _rc

    async def _main():
        await _rc.connect()
        await run_outbox_sweeper()

    asyncio.run(_main())
