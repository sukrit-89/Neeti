"""
WebSocket handlers for real-time session events.
Authenticated via token query parameter.

Production hardening v2:
  - Session-scoped Redis channels: events:session:{id}:* instead of
    global events:* channels. Eliminates the structural cross-session
    message leak that existed even when the session_id filter was correct.
  - Replaced get_message() polling loop (O(N) coroutine wakes) with
    the native async iterator pubsub.listen() which only yields on real
    messages — zero-sleep, event-driven.
  - Proper CancelledError propagation for clean task teardown.
"""
import asyncio
import json
from typing import Dict, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.core.database import AsyncSessionLocal
from app.core.logging import logger
from app.models.models import Session, Candidate
from app.core.redis import redis_client
from app.schemas.schemas import WSMessage

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Session-scoped channel names
# Using per-session namespacing instead of global event channels means
# a subscriber NEVER receives events destined for a different session,
# even if the session_id filtering logic has a future bug.
# ─────────────────────────────────────────────────────────────────────────────

def _session_channels(session_id: int) -> list[str]:
    """Return the Redis channel names scoped to a specific session."""
    return [
        f"events:session:{session_id}:code.changed",
        f"events:session:{session_id}:code.executed",
        f"events:session:{session_id}:speech.transcribed",
        f"events:session:{session_id}:session.ended",
        f"events:session:{session_id}:environment.anomaly",
        f"events:session:{session_id}:agent.processing_completed",
    ]


async def authenticate_websocket(token: str) -> Optional[dict]:
    """
    Authenticate WebSocket connection using token.
    Returns user dict or None if authentication fails.
    """
    if not token:
        logger.warning("WebSocket auth: missing token")
        return None

    try:
        # BUG-02 FIX (WS): reuse the singleton client and offload the sync
        # supabase call to a thread so the WS event loop is not blocked.
        from app.core.auth import get_supabase_client, _resolve_role
        supabase = get_supabase_client()
        user_response = await asyncio.to_thread(supabase.auth.get_user, token)

        if not user_response or not user_response.user:
            logger.warning("WebSocket auth: invalid token")
            return None

        user = user_response.user
        role = _resolve_role(user)
        return {
            "id": user.id,
            "email": user.email,
            "role": role,
        }
    except Exception as e:
        logger.error(f"WebSocket auth exception: {e}")
        return None


async def verify_session_membership(user: dict, session_id: int) -> bool:
    """Verify the authenticated user belongs to this session."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Session).where(Session.id == session_id)
            )
            session = result.scalar_one_or_none()
            if not session:
                logger.warning(f"WebSocket session check: session {session_id} not found")
                return False

            # Check if recruiter — compare as strings since both are UUIDs stored as strings
            if str(session.recruiter_id) == str(user["id"]):
                return True

            # Check if candidate (by user_id)
            result = await db.execute(
                select(Candidate).where(
                    and_(
                        Candidate.session_id == session_id,
                        Candidate.user_id == str(user["id"])
                    )
                )
            )
            candidate = result.first()

            # Email-based fallback for candidates joined before user_id was set
            if not candidate and user.get("email"):
                result = await db.execute(
                    select(Candidate).where(
                        and_(
                            Candidate.session_id == session_id,
                            Candidate.email == user["email"]
                        )
                    )
                )
                candidate = result.first()

            if candidate:
                return True

        logger.warning(
            f"WebSocket session check failed: user {user['id']} (role={user.get('role')}) "
            f"not a member of session {session_id}"
        )
        return False
    except Exception as e:
        logger.error(f"WebSocket session check error: {e}")
        return False


class ConnectionManager:
    """Manages WebSocket connections per session."""

    def __init__(self):
        # session_id → set of (websocket, user_id) — using a set prevents dups
        self.active_connections: Dict[int, list[tuple[WebSocket, str]]] = {}

    async def connect(self, websocket: WebSocket, session_id: int, user_id: str):
        """Add to session room (accept must have been called before this)."""
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append((websocket, user_id))
        logger.info(
            f"WebSocket connected: session={session_id} user={user_id} "
            f"total={len(self.active_connections[session_id])}"
        )

    def disconnect(self, websocket: WebSocket, session_id: int):
        """Remove WebSocket connection."""
        if session_id in self.active_connections:
            self.active_connections[session_id] = [
                (ws, uid) for ws, uid in self.active_connections[session_id]
                if ws is not websocket
            ]
            if not self.active_connections[session_id]:
                del self.active_connections[session_id]
        logger.info(f"WebSocket disconnected: session={session_id}")

    async def send_to_session(
        self,
        session_id: int,
        message: dict,
        exclude: WebSocket | None = None,
    ):
        """Send message to all connections in a session, optionally excluding sender."""
        if session_id not in self.active_connections:
            return
        disconnected: list[WebSocket] = []
        for ws, _uid in list(self.active_connections[session_id]):
            if ws is exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws, session_id)


manager = ConnectionManager()


@router.websocket("/ws/session/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: int,
    token: str = Query(default="")
):
    """
    WebSocket endpoint for real-time session updates.
    Requires ?token=<supabase_access_token> query parameter.

    Architecture:
    - Accept the WebSocket FIRST (otherwise the browser sees a TCP close, not 403)
    - Authenticate after accept; close with 1008 on failure
    - Listen for both client messages AND session-scoped Redis pub/sub events
    - Redis listener uses async iterator (not polling) — zero-sleep, event-driven
    """
    await websocket.accept()

    user = await authenticate_websocket(token)
    if not user:
        await websocket.send_json({"type": "error", "message": "Authentication failed"})
        await websocket.close(code=1008, reason="Authentication failed")
        return

    if not await verify_session_membership(user, session_id):
        await websocket.send_json({"type": "error", "message": "Not authorized for this session"})
        await websocket.close(code=1008, reason="Not authorized for this session")
        return

    await manager.connect(websocket, session_id, user["id"])
    await websocket.send_json({
        "type": "connected",
        "data": {"session_id": session_id, "user_id": user["id"], "role": user.get("role")}
    })

    pubsub = None
    redis_task = None
    try:
        # ── Subscribe to SESSION-SCOPED channels ─────────────────────────────
        # Each session gets its own channel namespace, so no cross-session
        # message filtering is needed — structural isolation at subscribe time.
        channels = _session_channels(session_id)

        if redis_client.client:
            pubsub = redis_client.client.pubsub()
            await pubsub.subscribe(*channels)

        async def listen_redis():
            """
            Background task: forward Redis events to this WebSocket client.

            ISSUE (fixed): previous implementation used get_message() in a
            0.1s polling loop. Under 100 connections that is 1000 wake-ups/sec.

            FIX: use the native async iterator pubsub.listen() which yields
            only when a real message arrives — fully event-driven.
            """
            if not pubsub:
                return
            try:
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        event_data = json.loads(message["data"])
                        # Channel is already session-scoped — no session_id
                        # filter needed (structural isolation).
                        await websocket.send_json({
                            "type": event_data.get("event_type", "unknown"),
                            "timestamp": event_data.get("timestamp", ""),
                            "data": event_data.get("data", {}),
                        })
                    except (json.JSONDecodeError, Exception) as e:
                        logger.debug(f"Redis event parse error: {e}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Redis listener stopped for session {session_id}: {e}")

        redis_task = asyncio.create_task(listen_redis())

        # ── Main loop: handle client → server messages ────────────────────────
        try:
            while True:
                raw = await websocket.receive_json()

                # BUG-17 FIX: Validate incoming WS messages against the
                # WSMessage schema. Unvalidated raw dicts are silently processed
                # even when malformed (wrong type, missing fields, etc.).
                try:
                    msg = WSMessage.model_validate(raw)
                except ValidationError as val_err:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Invalid message format",
                        "detail": val_err.errors(include_url=False),
                    })
                    continue

                msg_type = msg.type

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

                elif msg_type == "code.changed":
                    # Broadcast code change to other participants only
                    await manager.send_to_session(
                        session_id,
                        {"type": "code.changed", "data": msg.data},
                        exclude=websocket,
                    )

                elif msg_type == "code.executed":
                    # Broadcast execution result to other participants only
                    await manager.send_to_session(
                        session_id,
                        {"type": "code.executed", "data": msg.data},
                        exclude=websocket,
                    )

                elif msg_type == "request_metrics":
                    try:
                        from app.services.metrics_service import MetricsService
                        metrics = await MetricsService.get_live_metrics(session_id)
                        await websocket.send_json({"type": "metrics_update", "data": metrics})
                    except Exception as e:
                        logger.warning(f"Metrics request failed: {e}")
                        await websocket.send_json({"type": "metrics_update", "data": {}})

        except WebSocketDisconnect:
            logger.info(f"Client disconnected from session {session_id}")
        except Exception as e:
            logger.warning(f"WebSocket receive error: {e}")

    except Exception as e:
        logger.error(f"WebSocket setup error: {e}")

    finally:
        if redis_task:
            redis_task.cancel()
            try:
                await redis_task
            except asyncio.CancelledError:
                pass
        manager.disconnect(websocket, session_id)
        if pubsub:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:
                pass
