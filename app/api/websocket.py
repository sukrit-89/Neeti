"""
WebSocket handlers for real-time session events.
Authenticated via token query parameter.
"""
import asyncio
import json
from typing import Dict, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.core.database import AsyncSessionLocal
from app.core.logging import logger
from app.models.models import Session, Candidate
from app.core.redis import redis_client

router = APIRouter()


async def authenticate_websocket(token: str) -> Optional[dict]:
    """
    Authenticate WebSocket connection using token.
    Returns user dict or None if authentication fails.
    """
    if not token:
        logger.warning("WebSocket auth: missing token")
        return None

    try:
        from app.core.config import settings
        if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
            logger.error("WebSocket auth: Supabase not configured")
            return None

        from supabase import create_client
        supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
        user_response = supabase.auth.get_user(token)

        if not user_response or not user_response.user:
            logger.warning("WebSocket auth: invalid token")
            return None

        user = user_response.user
        return {
            "id": user.id,
            "email": user.email,
            "role": user.user_metadata.get("role", "candidate") if user.user_metadata else "candidate",
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

            # Check if candidate
            result = await db.execute(
                select(Candidate).where(
                    and_(
                        Candidate.session_id == session_id,
                        Candidate.user_id == str(user["id"])
                    )
                )
            )
            candidate = result.first()
            
            # ── FIX #22: Email-based fallback for candidates (consistent with REST API) ──
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
            f"not a member of session {session_id} (recruiter_id={session.recruiter_id})"
        )
        return False
    except Exception as e:
        logger.error(f"WebSocket session check error: {e}")
        return False


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self):
        self.active_connections: Dict[int, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, session_id: int):
        """Add to session room (accept must be called before this)."""
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)
        logger.info(f"WebSocket connected to session {session_id} (total: {len(self.active_connections[session_id])})")

    def disconnect(self, websocket: WebSocket, session_id: int):
        """Remove WebSocket connection."""
        if session_id in self.active_connections:
            if websocket in self.active_connections[session_id]:
                self.active_connections[session_id].remove(websocket)
            if not self.active_connections[session_id]:
                del self.active_connections[session_id]
        logger.info(f"WebSocket disconnected from session {session_id}")

    async def send_to_session(self, session_id: int, message: dict, exclude: WebSocket | None = None):
        """Send message to all connections in a session, optionally excluding the sender."""
        if session_id in self.active_connections:
            disconnected = []
            for connection in self.active_connections[session_id]:
                if connection is exclude:
                    continue
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.append(connection)
            for conn in disconnected:
                self.disconnect(conn, session_id)


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
    - Accept the WebSocket FIRST (otherwise the browser sees 403)
    - Authenticate after accept
    - If auth fails, send error message then close
    - Listen for both client messages AND Redis pub/sub events
    """
    # MUST accept first — closing before accept = HTTP 403
    await websocket.accept()

    # Authenticate
    user = await authenticate_websocket(token)
    if not user:
        await websocket.send_json({"type": "error", "message": "Authentication failed"})
        await websocket.close(code=1008, reason="Authentication failed")
        return

    # Verify membership
    if not await verify_session_membership(user, session_id):
        await websocket.send_json({"type": "error", "message": "Not authorized for this session"})
        await websocket.close(code=1008, reason="Not authorized for this session")
        return

    # Connected and authorized
    await manager.connect(websocket, session_id)
    await websocket.send_json({
        "type": "connected",
        "data": {"session_id": session_id, "user_id": user["id"], "role": user.get("role")}
    })

    # Set up Redis pub/sub for code events
    pubsub = None
    try:
        # Subscribe to relevant Redis channels for this session's events
        channels = [
            f"events:code.changed",
            f"events:code.executed",
            f"events:speech.transcribed",
            f"events:session.ended",
            f"events:environment.anomaly",
        ]

        if redis_client.client:
            pubsub = redis_client.client.pubsub()
            await pubsub.subscribe(*channels)

        async def listen_redis():
            """Background task: forward Redis events to WebSocket."""
            if not pubsub:
                return
            try:
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message and message["type"] == "message":
                        try:
                            event_data = json.loads(message["data"])
                            # Only forward events for THIS session
                            if event_data.get("session_id") == session_id:
                                await websocket.send_json({
                                    "type": event_data.get("event_type", "unknown"),
                                    "timestamp": event_data.get("timestamp", ""),
                                    "data": event_data.get("data", {})
                                })
                        except (json.JSONDecodeError, Exception) as e:
                            logger.debug(f"Redis event parse error: {e}")
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Redis listener stopped: {e}")

        # Start Redis listener as background task
        redis_task = asyncio.create_task(listen_redis())

        # Main loop: listen for client messages
        try:
            while True:
                data = await websocket.receive_json()
                msg_type = data.get("type", "")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

                elif msg_type == "code.changed":
                    # Candidate sent code update — broadcast to recruiter, EXCLUDE sender
                    await manager.send_to_session(session_id, {
                        "type": "code.changed",
                        "data": data.get("data", {}),
                    }, exclude=websocket)

                elif msg_type == "code.executed":
                    # Candidate executed code — broadcast output to recruiter, EXCLUDE sender
                    await manager.send_to_session(session_id, {
                        "type": "code.executed",
                        "data": data.get("data", {}),
                    }, exclude=websocket)

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
        finally:
            # Cancel the Redis listener
            redis_task.cancel()
            try:
                await redis_task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error(f"WebSocket setup error: {e}")

    finally:
        # Clean up
        manager.disconnect(websocket, session_id)
        if pubsub:
            try:
                await pubsub.unsubscribe()
                await pubsub.close()
            except Exception:
                pass
