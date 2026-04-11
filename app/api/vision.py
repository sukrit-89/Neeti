"""
Vision API endpoints.
Receives video frames from the frontend, runs MediaPipe analysis,
and persists results to vision_metrics table.
"""
import base64
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from pydantic import BaseModel
from typing import Optional

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.models import Session, Candidate, VisionMetric
from app.services.vision_service import vision_service
from app.core.logging import logger

router = APIRouter(prefix="/vision", tags=["Vision"])


class FrameAnalysisRequest(BaseModel):
    """Request body for frame analysis."""
    session_id: int
    frame_data: str  # base64-encoded JPEG frame
    timestamp_offset: float = 0.0  # seconds since session start


class VisionStatusResponse(BaseModel):
    """Vision service status."""
    configured: bool
    service: str
    mediapipe_available: bool


async def _verify_session_participant(
    session_id: int, current_user: dict, db: AsyncSession
) -> Session:
    """Verify user is a participant of the session."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    user_id = current_user.get("id") or current_user.get("sub")
    role = current_user.get("role", "candidate")

    if role == "recruiter":
        if str(session.recruiter_id) != str(user_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised")
    else:
        candidate = await db.execute(
            select(Candidate).where(
                and_(Candidate.session_id == session_id, Candidate.user_id == str(user_id))
            )
        )
        cand = candidate.first()
        
        if not cand and current_user.get("email"):
            candidate = await db.execute(
                select(Candidate).where(
                    and_(Candidate.session_id == session_id, Candidate.email == current_user["email"])
                )
            )
            cand = candidate.first()
            
        if not cand:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled")

    return session


@router.post("/analyze-frame")
async def analyze_frame(
    request: FrameAnalysisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Analyze a single video frame for behavior metrics.
    
    The frontend captures webcam frames periodically (every 2-3s)
    and sends them as base64-encoded JPEG. Results are persisted
    to vision_metrics for the VisionAgent to analyze after session ends.
    """
    session = await _verify_session_participant(
        request.session_id, current_user, db
    )

    # Decode base64 frame
    try:
        frame_bytes = base64.b64decode(request.frame_data)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid frame data: {e}"
        )

    # Run MediaPipe analysis
    analysis = await vision_service.analyze_frame(frame_bytes)

    # --- Persist metrics to vision_metrics table ---
    metrics_saved = 0

    # 1. Gaze / eye contact metric
    if analysis.get("face_detected"):
        head_pose = analysis.get("head_pose", "unknown")
        gaze_label = "focused" if head_pose == "forward" else "looking_away"
        
        gaze_metric = VisionMetric(
            session_id=request.session_id,
            metric_type="gaze",
            value=analysis.get("eye_contact_score", 0.0),
            label=gaze_label,
            confidence=analysis.get("eye_contact_score", 0.0),
            meta_data={
                "head_pose": head_pose,
                "face_detected": True,
                "multiple_faces": analysis.get("multiple_faces", False),
            },
        )
        db.add(gaze_metric)
        metrics_saved += 1

    # 2. Emotion metric
    emotion = analysis.get("emotion", "neutral")
    emotion_label = "focused" if emotion in ("neutral", "focused") else emotion
    
    emotion_metric = VisionMetric(
        session_id=request.session_id,
        metric_type="emotion",
        value=analysis.get("engagement_score", 0.0),
        label=emotion_label,
        confidence=0.7,
        meta_data={"raw_emotion": emotion},
    )
    db.add(emotion_metric)
    metrics_saved += 1

    # 3. Presence metric
    presence_value = 1.0 if analysis.get("face_detected", False) else 0.0
    presence_metric = VisionMetric(
        session_id=request.session_id,
        metric_type="presence",
        value=presence_value,
        label="present" if presence_value == 1.0 else "absent",
        confidence=0.9,
        meta_data={
            "suspicious_behavior": analysis.get("suspicious_behavior", False),
        },
    )
    db.add(presence_metric)
    metrics_saved += 1

    await db.commit()

    logger.info(
        f"Vision frame analyzed: session={request.session_id}, "
        f"face={analysis.get('face_detected')}, "
        f"engagement={analysis.get('engagement_score', 0):.2f}, "
        f"metrics_saved={metrics_saved}"
    )

    return {
        "success": True,
        "face_detected": analysis.get("face_detected", False),
        "engagement_score": analysis.get("engagement_score", 0.0),
        "head_pose": analysis.get("head_pose", "unknown"),
        "suspicious_behavior": analysis.get("suspicious_behavior", False),
        "metrics_saved": metrics_saved,
    }


@router.get("/status")
async def get_vision_status() -> dict:
    """Get vision service configuration status."""
    from app.services.vision_service import MEDIAPIPE_AVAILABLE

    return {
        "configured": MEDIAPIPE_AVAILABLE,
        "service": "mediapipe" if MEDIAPIPE_AVAILABLE else "fallback",
        "mediapipe_available": MEDIAPIPE_AVAILABLE,
        "note": (
            "MediaPipe active — face detection, gaze tracking, and pose estimation enabled."
            if MEDIAPIPE_AVAILABLE
            else "MediaPipe not installed. Using fallback analysis. Install with: pip install mediapipe"
        ),
    }
