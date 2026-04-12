"""
Speech API endpoints.
Handle audio transcription and speech analysis.
Persists transcription results to speech_segments table.
"""
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.models import User, Session, Candidate, SpeechSegment
from app.services.speech_service import speech_service
from app.core.logging import logger
from app.core.events import EventPublisher, Event, EventType

router = APIRouter(prefix="/speech", tags=["Speech"])


async def verify_session_participant(session_id: int, current_user: dict, db: AsyncSession) -> Session:
    """Verify user is a participant (recruiter or enrolled candidate) of the session."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    user_id = current_user.get("id") or current_user.get("sub")
    role = current_user.get("role", "candidate")

    if role == "recruiter":
        if str(session.recruiter_id) != str(user_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised for this session")
    else:
        candidate = await db.execute(
            select(Candidate).where(
                and_(Candidate.session_id == session_id, Candidate.user_id == str(user_id))
            )
        )
        cand = candidate.scalar_one_or_none()
        
        if not cand and current_user.get("email"):
            candidate = await db.execute(
                select(Candidate).where(
                    and_(Candidate.session_id == session_id, Candidate.email == current_user["email"])
                )
            )
            cand = candidate.scalar_one_or_none()
            
        if not cand:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this session")

    return session

@router.post("/transcribe")
async def transcribe_audio(
    session_id: int = Form(...),
    audio: UploadFile = File(...),
    start_time: float = Form(0.0),
    duration: float = Form(0.0),
    language: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> dict:
    """
    Transcribe audio from interview and persist to speech_segments.
    
    The frontend sends audio chunks periodically during the interview.
    Each chunk is transcribed and stored as a SpeechSegment row.
    """
    
    session = await verify_session_participant(session_id, current_user, db)
    
    try:
        audio_bytes = await audio.read()
    except Exception as e:
        logger.error(f"Failed to read audio file: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid audio file"
        )
    
    transcription_result = await speech_service.transcribe_audio(
        audio_file=audio_bytes,
        language=language
    )
    
    if not transcription_result["success"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=transcription_result.get("error", "Transcription failed")
        )
    
    text = transcription_result["text"].strip()
    
    # --- Persist to speech_segments ---
    if text:
        user_id = current_user.get("id") or current_user.get("sub")
        end_time = start_time + duration if duration > 0 else start_time + 5.0
        actual_duration = duration if duration > 0 else 5.0
        
        segment = SpeechSegment(
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            duration=actual_duration,
            transcript=text,
            language=transcription_result.get("language") or language or "en",
            confidence=transcription_result.get("confidence", 0.0),
            speaker_id=str(user_id),
        )
        db.add(segment)
        await db.commit()
        await db.refresh(segment)
        
        logger.info(
            f"Speech segment saved: session={session_id}, "
            f"duration={actual_duration:.1f}s, words={len(text.split())}"
        )
        
        # Publish event for real-time broadcast
        event = Event(
            event_type=EventType.SPEECH_TRANSCRIBED,
            session_id=session_id,
            data={
                "segment_id": segment.id,
                "text": text,
                # BUG-01 FIX: use .get() — key may be absent from service result
                "confidence": transcription_result.get("confidence", 0.0),
                "duration": actual_duration,
                "speaker_id": str(user_id),
            }
        )
        await EventPublisher.publish(event)
    
    # Guard: 'segment' is only defined when text is non-empty.
    # BUG-01 FIX: transcription_result["confidence"] raises KeyError when
    # the speech service omits the key — use .get() with a safe default
    # everywhere on this dict.
    confidence = transcription_result.get("confidence", 0.0)
    return {
        "success": True,
        "text": text,
        "confidence": confidence,
        "language": transcription_result.get("language"),
        "segment_id": segment.id if text else None,
    }


@router.post("/segment")
async def save_speech_segment(
    session_id: int = Form(...),
    transcript: str = Form(...),
    start_time: float = Form(0.0),
    duration: float = Form(5.0),
    confidence: float = Form(0.85),
    language: Optional[str] = Form("en"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Save a pre-transcribed speech segment (from browser Web Speech API).
    
    This is the primary endpoint used when server-side Whisper is not available.
    The browser does the transcription via SpeechRecognition API and just sends
    the text here for persistence.
    """
    session = await verify_session_participant(session_id, current_user, db)
    
    text = transcript.strip()
    if not text:
        return {"success": True, "segment_id": None, "message": "Empty transcript, skipped"}
    
    user_id = current_user.get("id") or current_user.get("sub")
    end_time = start_time + duration
    
    segment = SpeechSegment(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        duration=duration,
        transcript=text,
        language=language or "en",
        confidence=confidence,
        speaker_id=str(user_id),
    )
    db.add(segment)
    await db.commit()
    await db.refresh(segment)
    
    logger.info(
        f"Speech segment saved (browser STT): session={session_id}, "
        f"words={len(text.split())}, duration={duration:.1f}s"
    )
    
    return {
        "success": True,
        "segment_id": segment.id,
        "word_count": len(text.split()),
    }

@router.post("/analyze")
async def analyze_speech(
    session_id: int = Form(...),
    transcription: str = Form(...),
    duration: float = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> dict:
    """Analyze speech quality metrics."""
    
    await verify_session_participant(session_id, current_user, db)
    
    analysis = await speech_service.analyze_speech_quality(
        transcription=transcription,
        audio_duration=duration
    )
    
    logger.info(
        f"Speech analyzed for session {session_id}: "
        f"Clarity={analysis['clarity_score']}, WPM={analysis['words_per_minute']}"
    )
    
    return {
        "success": True,
        **analysis
    }

@router.get("/status")
async def get_speech_status() -> dict:
    """Get speech service configuration status."""
    
    from app.core.config import settings
    
    status_info = {
        "configured": False,
        "service": "none",
        "model": None
    }
    
    if settings.USE_LOCAL_WHISPER:
        try:
            import whisper
            status_info["configured"] = True
            status_info["service"] = "whisper-local"
            status_info["model"] = settings.WHISPER_MODEL
        except ImportError:
            status_info["service"] = "whisper-local (not installed)"
    
    elif settings.OPENAI_API_KEY:
        status_info["configured"] = True
        status_info["service"] = "openai-whisper"
        status_info["model"] = "whisper-1"
    
    return status_info
