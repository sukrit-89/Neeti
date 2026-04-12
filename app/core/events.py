"""
Event system for agent communication.
Decoupled, event-driven architecture.

Change: EventPublisher.publish now writes to TWO Redis channels:
  1. Global `events:{type}` - consumed by outbox sweeper (backward compat)
  2. Session-scoped `events:session:{id}:{type}` - consumed by the per-session
     WebSocket handler, providing structural cross-session isolation.
"""
import json
from datetime import datetime
from typing import Any, Optional
from enum import Enum

from pydantic import BaseModel, Field

from app.core.redis import redis_client
from app.core.logging import logger


class EventType(str, Enum):
    """System event types."""
    SESSION_CREATED = "session.created"
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"

    RECORDING_STARTED = "recording.started"
    RECORDING_STOPPED = "recording.stopped"

    CANDIDATE_JOINED = "candidate.joined"
    CANDIDATE_LEFT = "candidate.left"

    CODE_CHANGED = "code.changed"
    CODE_EXECUTED = "code.executed"

    SPEECH_DETECTED = "speech.detected"
    SPEECH_TRANSCRIBED = "speech.transcribed"

    VISION_METRIC_CAPTURED = "vision.metric_captured"

    AGENT_PROCESSING_STARTED = "agent.processing_started"
    AGENT_PROCESSING_COMPLETED = "agent.processing_completed"
    AGENT_PROCESSING_FAILED = "agent.processing_failed"

    EVALUATION_REQUESTED = "evaluation.requested"
    EVALUATION_COMPLETED = "evaluation.completed"

    ENVIRONMENT_ANOMALY = "environment.anomaly"


class Event(BaseModel):
    """Base event model."""
    event_type: EventType
    session_id: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventPublisher:
    """Publishes events to Redis channels."""

    @staticmethod
    async def publish(event: Event) -> None:
        """
        Publish event to appropriate channels.

        Writes to TWO channels atomically using a pipeline:
          1. Global channel `events:{type}` — for outbox sweeper and
             legacy consumers. Backward-compatible.
          2. Session-scoped channel `events:session:{id}:{type}` — for
             the per-session WebSocket handler. Structural isolation
             means the WS handler subscribes only to its own session's
             events and needs no application-level session_id filter.
        """
        global_channel = f"events:{event.event_type.value}"
        session_channel = f"events:session:{event.session_id}:{event.event_type.value}"

        try:
            if redis_client.client is None:
                logger.debug(f"Redis not connected, skipping event: {event.event_type.value}")
                return
            payload = json.dumps(event.model_dump(mode='json'))
            async with redis_client.client.pipeline(transaction=False) as pipe:
                pipe.publish(global_channel, payload)
                pipe.publish(session_channel, payload)
                await pipe.execute()
            logger.info(
                f"Published event: {event.event_type.value} for session {event.session_id}"
            )
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")


class EventSubscriber:
    """Subscribes to events from Redis channels."""

    def __init__(self, event_types: list[EventType]):
        self.event_types = event_types
        self.channels = [f"events:{et.value}" for et in event_types]
        self.pubsub = None

    async def subscribe(self):
        """Start subscription."""
        self.pubsub = await redis_client.subscribe(*self.channels)
        logger.info(f"Subscribed to channels: {self.channels}")

    async def listen(self):
        """Listen for events."""
        if not self.pubsub:
            await self.subscribe()

        async for message in self.pubsub.listen():
            if message["type"] == "message":
                try:
                    event_data = json.loads(message["data"])
                    yield Event(**event_data)
                except Exception as e:
                    logger.error(f"Error parsing event: {e}")

    async def unsubscribe(self):
        """Stop subscription."""
        if self.pubsub:
            await self.pubsub.unsubscribe(*self.channels)
            logger.info("Unsubscribed from event channels")


# ── Convenience helpers ────────────────────────────────────────────────────────

async def publish_session_created(session_id: int, data: dict[str, Any]) -> None:
    """Publish session created event."""
    await EventPublisher.publish(Event(
        event_type=EventType.SESSION_CREATED, session_id=session_id, data=data
    ))


async def publish_session_started(session_id: int, data: dict[str, Any]) -> None:
    """Publish session started event."""
    await EventPublisher.publish(Event(
        event_type=EventType.SESSION_STARTED, session_id=session_id, data=data
    ))


async def publish_session_ended(session_id: int, data: dict[str, Any]) -> None:
    """Publish session ended event."""
    await EventPublisher.publish(Event(
        event_type=EventType.SESSION_ENDED, session_id=session_id, data=data
    ))


async def publish_recording_started(session_id: int, recording_url: str) -> None:
    """Publish recording started event."""
    await EventPublisher.publish(Event(
        event_type=EventType.RECORDING_STARTED,
        session_id=session_id,
        data={"recording_url": recording_url},
    ))


async def publish_agent_completed(
    session_id: int,
    agent_type: str,
    output_id: int,
) -> None:
    """Publish agent processing completed event."""
    await EventPublisher.publish(Event(
        event_type=EventType.AGENT_PROCESSING_COMPLETED,
        session_id=session_id,
        data={"agent_type": agent_type, "output_id": output_id},
    ))


async def publish_evaluation_requested(session_id: int) -> None:
    """Publish evaluation requested event."""
    await EventPublisher.publish(Event(
        event_type=EventType.EVALUATION_REQUESTED, session_id=session_id, data={}
    ))


async def publish_code_changed(session_id: int, code: str = "", language: str = "", data: dict = None) -> None:
    """Publish code changed event."""
    event_data = data if data else {"code": code, "language": language}
    await EventPublisher.publish(Event(
        event_type=EventType.CODE_CHANGED, session_id=session_id, data=event_data
    ))


async def publish_code_executed(session_id: int, result: dict[str, Any] = None, data: dict = None) -> None:
    """Publish code executed event."""
    event_data = data if data else (result or {})
    await EventPublisher.publish(Event(
        event_type=EventType.CODE_EXECUTED, session_id=session_id, data=event_data
    ))


async def publish_environment_anomaly(session_id: int, data: dict) -> None:
    """Publish an environment.anomaly event for recruiter warnings."""
    await EventPublisher.publish(Event(
        event_type=EventType.ENVIRONMENT_ANOMALY, session_id=session_id, data=data
    ))
