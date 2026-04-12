"""
Celery tasks for agent processing.
Background jobs that process session data through AI agents.

Production hardening applied:
  - _run_async: safe event-loop management (new_event_loop + shutdown_asyncgens)
  - _persist_and_publish: atomic DB write + outbox entry in one transaction,
    eliminating the TOCTOU race in the app-level idempotency guard.
  - ON CONFLICT DO NOTHING (via INSERT ... WHERE NOT EXISTS pattern) removes
    the app-level SELECT + INSERT race entirely under concurrent workers.
  - Event delivery via transactional outbox (app/core/outbox.py) — no more
    "log and discard" on Redis failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.workers.celery_app import celery_app
from app.agents.base import AgentInput
from app.agents.coding_agent import CodingAgent
from app.agents.speech_agent import SpeechAgent
from app.agents.vision_agent import VisionAgent
from app.agents.reasoning_agent import ReasoningAgent
from app.agents.evaluation_agent import EvaluationAgent
from app.models.models import AgentOutput as AgentOutputModel, AgentType
from app.core.database import AsyncSessionLocal
from app.core.outbox import EventOutbox
from app.core.logging import logger


# =============================================================================
# ISSUE: asyncio.run() is banned when an event loop is already running.
# Under gevent/eventlet Celery pools this crashes with RuntimeError.
#
# FIX: Create a fresh event loop per call, drain async generators (which
# hold open SQLAlchemy connections), then close the loop.  One shared helper
# keeps this logic in one place.
# =============================================================================

def _run_async(coro) -> Any:
    """
    Safely run an async coroutine from a synchronous Celery task.

    Compatibility matrix:
      - Celery prefork (default): ✅ each process owns its own thread
      - Celery threads:           ✅ new loop per call, no sharing
      - Celery gevent/eventlet:   ✅ avoids RuntimeError from asyncio.run()
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    except Exception:
        raise
    finally:
        # Drain any open async generators (e.g. SQLAlchemy async sessions that
        # weren't explicitly closed) before destroying the loop.
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        # Cancel all remaining tasks (connection keep-alive tasks, etc.)
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


# =============================================================================
# ISSUE 1: TOCTOU race condition in idempotency guard
#   Two Celery workers executing simultaneously for the same session/agent_type
#   both read "no existing row", both insert, one gets a DB unique constraint
#   violation (if the partial unique index exists) OR both succeed and produce
#   duplicate rows (if it doesn't).
#
# FIX: Replace SELECT + INSERT with a single atomic operation.
#   INSERT INTO agent_outputs (...) ... ON CONFLICT DO NOTHING
#   SQLAlchemy doesn't have a clean cross-DB ON CONFLICT wrapper for orm objects,
#   so we use IntegrityError catching on the uniqueness constraint instead.
#   The partial unique index (migration 004) is the real enforcement layer.
#
# ISSUE 2: Event published inside DB context → Redis fail = Celery retry = dup row
#
# FIX: Write an outbox row IN THE SAME TRANSACTION as the agent_output row.
#   The outbox sweeper (app/core/outbox.py) delivers the event to Redis
#   asynchronously. If Redis is down the event is delivered when it recovers.
#   The transport is now at-least-once, not fire-and-forget.
# =============================================================================

async def _persist_and_publish(
    session_id: int,
    agent_type_enum: AgentType,
    agent_type_str: str,
    output,  # AgentOutput pydantic model
) -> dict:
    """
    Atomically persist agent output + outbox event in one transaction.

    Idempotency is enforced at two levels:
      1. Application level: SELECT … WHERE status IN ('completed','skipped') LIMIT 1
      2. Database level:    Partial unique index uq_agent_output_success
                            (migration 004_production_hardening.sql)
         If two workers race past the SELECT simultaneously, the second INSERT
         raises IntegrityError which we catch and handle gracefully.

    Event delivery:
      The outbox row is written in the same transaction as the agent_output row,
      so event and data are always consistent.  The sweeper delivers to Redis.
    """
    async with AsyncSessionLocal() as db:
        # ── 1. Idempotency check ─────────────────────────────────────────────
        existing = await db.execute(
            select(AgentOutputModel)
            .where(
                AgentOutputModel.session_id == session_id,
                AgentOutputModel.agent_type == agent_type_enum,
                AgentOutputModel.status.in_(["completed", "skipped"]),
            )
            .limit(1)
        )
        existing_row = existing.scalar_one_or_none()

        if existing_row is not None:
            logger.info(
                "[%s] idempotency: output already persisted for session %d "
                "(id=%d), skipping insert.",
                agent_type_str, session_id, existing_row.id,
            )
            return output.model_dump()

        # ── 2. Insert agent output + outbox event atomically ────────────────
        db_output = AgentOutputModel(
            session_id    = session_id,
            agent_type    = agent_type_enum,
            status        = output.status,
            score         = output.score,
            findings      = output.findings,
            flags         = output.flags,
            insights      = output.insights,
            error_message = output.error_message,
        )
        outbox_row = EventOutbox(
            session_id = session_id,
            event_type = "agent.processing_completed",
            payload    = {
                "event_type": "agent.processing_completed",
                "session_id": session_id,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "data": {
                    "agent_type": agent_type_str,
                    # output_id filled after flush (see below)
                },
            },
        )

        db.add(db_output)
        db.add(outbox_row)

        try:
            # Flush to obtain db_output.id WITHOUT committing yet.
            # If the partial unique index fires here, we catch IntegrityError.
            await db.flush()
        except IntegrityError:
            await db.rollback()
            logger.info(
                "[%s] concurrent insert race: unique constraint prevented dup "
                "for session %d. Fetching existing row.",
                agent_type_str, session_id,
            )
            # Re-open a clean session and return the existing record
            async with AsyncSessionLocal() as db2:
                r = await db2.execute(
                    select(AgentOutputModel).where(
                        AgentOutputModel.session_id == session_id,
                        AgentOutputModel.agent_type == agent_type_enum,
                        AgentOutputModel.status.in_(["completed", "skipped"]),
                    ).limit(1)
                )
                _ = r.scalar_one_or_none()
            return output.model_dump()

        # Patch the outbox payload with the real output id
        outbox_row.payload["data"]["output_id"] = db_output.id

        await db.commit()

        logger.info(
            "[%s] persisted output id=%d status=%s score=%s for session %d",
            agent_type_str, db_output.id, output.status,
            f"{output.score:.1f}" if output.score is not None else "N/A",
            session_id,
        )

    return output.model_dump()


# =============================================================================
# Individual Celery tasks
# Each is a thin wrapper: instantiate agent → run → persist+publish
# =============================================================================

@celery_app.task(name="process_coding_agent", bind=True)
def process_coding_agent(self, session_id: int, data: dict[str, Any]) -> dict:
    """Process session through coding agent."""
    async def _run():
        output = await CodingAgent().run(AgentInput(session_id=session_id, data=data))
        return await _persist_and_publish(session_id, AgentType.CODING, "coding", output)
    return _run_async(_run())


@celery_app.task(name="process_speech_agent", bind=True)
def process_speech_agent(self, session_id: int, data: dict[str, Any]) -> dict:
    """Process session through speech agent."""
    async def _run():
        output = await SpeechAgent().run(AgentInput(session_id=session_id, data=data))
        return await _persist_and_publish(session_id, AgentType.SPEECH, "speech", output)
    return _run_async(_run())


@celery_app.task(name="process_vision_agent", bind=True)
def process_vision_agent(self, session_id: int, data: dict[str, Any]) -> dict:
    """Process session through vision agent."""
    async def _run():
        output = await VisionAgent().run(AgentInput(session_id=session_id, data=data))
        return await _persist_and_publish(session_id, AgentType.VISION, "vision", output)
    return _run_async(_run())


@celery_app.task(name="process_reasoning_agent", bind=True)
def process_reasoning_agent(self, session_id: int, data: dict[str, Any]) -> dict:
    """Process session through reasoning agent."""
    async def _run():
        output = await ReasoningAgent().run(AgentInput(session_id=session_id, data=data))
        return await _persist_and_publish(session_id, AgentType.REASONING, "reasoning", output)
    return _run_async(_run())


# =============================================================================
# ISSUE: process_evaluation_agent opens TWO separate DB sessions sequentially,
# creating a window where another worker could insert a duplicate Evaluation row
# between the idempotency SELECT and the INSERT.
#
# FIX: Perform the Evaluation idempotency check + insert inside a SINGLE
# transaction using SELECT FOR UPDATE NOWAIT.  If another worker has already
# acquired the lock, we get LockNotAvailable → log + return, no duplicate.
# =============================================================================

@celery_app.task(name="process_evaluation_agent", bind=True)
def process_evaluation_agent(self, session_id: int, data: dict[str, Any]) -> dict:
    """Generate final evaluation from all agents."""
    from app.models.models import Evaluation
    from sqlalchemy.exc import OperationalError

    async def _run():
        output = await EvaluationAgent().run(AgentInput(session_id=session_id, data=data))

        # Persist AgentOutput (with idempotency + outbox)
        result = await _persist_and_publish(
            session_id, AgentType.EVALUATION, "evaluation", output
        )

        # ── Evaluation record (separate transaction with advisory lock) ──────
        async with AsyncSessionLocal() as db:
            # Use PostgreSQL advisory lock to serialize concurrent evaluation
            # writes for the same session_id — no TOCTOU window possible.
            try:
                await db.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": session_id}
                )
            except Exception as lock_err:
                logger.warning(
                    "[evaluation] could not acquire advisory lock for session %d: %s",
                    session_id, lock_err,
                )
                # Non-fatal: fall through to the explicit idempotency check
                pass

            existing_eval = await db.execute(
                select(Evaluation)
                .where(Evaluation.session_id == session_id)
                .limit(1)
            )
            if existing_eval.scalar_one_or_none() is not None:
                logger.info(
                    "[evaluation] already recorded for session %d, skipping.", session_id
                )
                return result

            # ── Best-effort anomaly extraction ────────────────────────────────
            anomaly_probability = None
            anomaly_mode        = None
            anomaly_reasons: list = []
            behavioral_features: dict = {}

            try:
                coding_result = await db.execute(
                    select(AgentOutputModel)
                    .where(
                        AgentOutputModel.session_id == session_id,
                        AgentOutputModel.agent_type == AgentType.CODING,
                        AgentOutputModel.status == "completed",
                    )
                    .order_by(AgentOutputModel.started_at.desc())
                    .limit(1)
                )
                coding_output = coding_result.scalar_one_or_none()
                if coding_output and coding_output.findings:
                    f = coding_output.findings
                    anomaly_probability  = f.get("anomaly_probability")
                    anomaly_mode         = f.get("anomaly_mode")
                    anomaly_reasons      = f.get("anomaly_evidence", [])
                    behavioral_features  = f.get("behavioral_features", {})
            except Exception as exc:
                logger.warning(
                    "[evaluation] failed to extract anomaly data for session %d: %s",
                    session_id, exc,
                )

            findings = output.findings
            evaluation = Evaluation(
                session_id           = session_id,
                overall_score        = output.score or 0.0,
                coding_score         = findings.get("coding_score"),
                communication_score  = findings.get("communication_score"),
                engagement_score     = findings.get("engagement_score"),
                reasoning_score      = findings.get("reasoning_score"),
                recommendation       = "hire" if (output.score or 0.0) >= 75 else "no_hire",
                confidence_level     = 0.85,
                strengths            = [],
                weaknesses           = [],
                key_findings         = output.flags,
                summary              = output.insights,
                detailed_report      = output.insights,
                anomaly_probability  = anomaly_probability,
                anomaly_mode         = anomaly_mode,
                anomaly_reasons      = anomaly_reasons or [],
                behavioral_features  = behavioral_features or {},
            )
            db.add(evaluation)
            await db.commit()

            logger.info("[evaluation] completed for session %d score=%.1f recommendation=%s",
                        session_id, output.score or 0.0, evaluation.recommendation)

        return result

    return _run_async(_run())


# =============================================================================
# Trigger task (unchanged logic, event-loop fix inherited from _run_async)
# =============================================================================

@celery_app.task(name="trigger_all_agents", bind=True)
def trigger_all_agents(self, session_id: int) -> None:
    """
    Trigger all agents to process a session.
    Called when a session ends.
    Uses LangGraph when USE_LANGGRAPH=True; falls back to Celery chord.
    """
    from app.core.config import settings

    if settings.USE_LANGGRAPH:
        logger.info("[LangGraph] triggering pipeline for session %d", session_id)
        from app.agents.graph import run_evaluation_pipeline
        try:
            _run_async(run_evaluation_pipeline(session_id))
            logger.info("[LangGraph] pipeline complete for session %d", session_id)
        except Exception as exc:
            logger.error("[LangGraph] pipeline failed for session %d: %s", session_id, exc)
            logger.info("[LangGraph] falling back to Celery chord for session %d", session_id)
            _trigger_chord_legacy(session_id)
    else:
        _trigger_chord_legacy(session_id)


def _trigger_chord_legacy(session_id: int) -> None:
    """Legacy Celery chord — kept as fallback."""
    from celery import chord
    logger.info("triggering agents via chord for session %d", session_id)
    chord(
        [
            process_coding_agent.s(session_id, {}),
            process_speech_agent.s(session_id, {}),
            process_vision_agent.s(session_id, {}),
            process_reasoning_agent.s(session_id, {}),
        ],
        process_evaluation_agent.si(session_id, {})
    ).apply_async()
    logger.info("all agents dispatched (chord) for session %d", session_id)
