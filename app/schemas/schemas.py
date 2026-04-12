"""
Pydantic schemas for request/response validation.
Using Pydantic v2 with production-ready patterns.
"""
from datetime import datetime, timezone
from typing import Optional, Any, Union
from pydantic import BaseModel, EmailStr, Field, ConfigDict, model_validator

from app.models.models import UserRole, SessionStatus, AgentType

class UserBase(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)
    role: UserRole

class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=100)

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: Union[int, str]
    email: EmailStr
    full_name: str = Field(default="User", min_length=1, max_length=255)
    role: UserRole
    is_active: bool
    created_at: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int

class SessionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    job_description: Optional[str] = Field(None, max_length=5000)
    scheduled_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class SessionUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    status: Optional[SessionStatus] = None

class SessionResponse(BaseModel):
    id: int
    session_code: str
    title: str
    description: Optional[str]
    recruiter_id: str
    status: SessionStatus
    scheduled_at: Optional[datetime]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    created_at: Optional[datetime] = None
    room_name: Optional[str]
    job_description: Optional[str] = None
    jd_profile: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="meta_data")
    
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

class SessionJoinRequest(BaseModel):
    session_code: str = Field(..., min_length=6, max_length=50)
    full_name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr

class SessionJoinResponse(BaseModel):
    session: SessionResponse
    room_token: str
    candidate_id: int

class RoomTokenResponse(BaseModel):
    room_token: str
    room_name: str
    participant_identity: str

class CandidateResponse(BaseModel):
    id: int
    session_id: int
    email: str
    full_name: str
    joined_at: Optional[datetime]
    is_present: bool
    created_at: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)

class CodingEventCreate(BaseModel):
    session_id: int
    event_type: str = Field(..., min_length=1, max_length=50)
    code_snapshot: Optional[str] = None
    language: Optional[str] = Field(None, max_length=50)
    execution_output: Optional[str] = None
    execution_error: Optional[str] = None
    execution_time_ms: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class CodingEventResponse(BaseModel):
    id: int
    session_id: int
    timestamp: datetime
    event_type: str
    code_snapshot: Optional[str]
    language: Optional[str]
    execution_output: Optional[str]
    execution_error: Optional[str]
    execution_time_ms: Optional[int]
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="meta_data")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

class SpeechSegmentCreate(BaseModel):
    start_time: float = Field(..., ge=0)
    end_time: float = Field(..., ge=0)
    duration: float = Field(..., ge=0)
    transcript: str = Field(..., min_length=1)
    language: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0, le=1)
    speaker_id: Optional[str] = None
    audio_url: Optional[str] = None

    # BUG-12 FIX: cross-field time consistency validation
    @model_validator(mode='after')
    def validate_time_range(self) -> 'SpeechSegmentCreate':
        if self.end_time < self.start_time:
            raise ValueError("end_time must be >= start_time")
        if self.duration > 0:
            expected = self.end_time - self.start_time
            if abs(self.duration - expected) > 0.5:  # 0.5s tolerance
                raise ValueError(
                    f"duration ({self.duration:.3f}s) is inconsistent with "
                    f"time range end_time - start_time = {expected:.3f}s"
                )
        return self

class SpeechSegmentResponse(BaseModel):
    id: int
    session_id: int
    start_time: float
    end_time: float
    duration: float
    transcript: str
    language: Optional[str]
    confidence: Optional[float]
    speaker_id: Optional[str]
    timestamp: datetime
    
    model_config = ConfigDict(from_attributes=True)

class VisionMetricCreate(BaseModel):
    metric_type: str = Field(..., min_length=1, max_length=50)
    value: Optional[float] = None
    label: Optional[str] = Field(None, max_length=100)
    confidence: Optional[float] = Field(None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

class VisionMetricResponse(BaseModel):
    id: int
    session_id: int
    timestamp: datetime
    metric_type: str
    value: Optional[float]
    label: Optional[str]
    confidence: Optional[float]
    metadata: dict[str, Any]
    
    model_config = ConfigDict(from_attributes=True)

class AgentOutputResponse(BaseModel):
    id: int
    session_id: int
    agent_type: AgentType
    started_at: datetime
    completed_at: Optional[datetime]
    status: str
    score: Optional[float]
    findings: dict[str, Any]
    flags: list[dict[str, Any]]
    insights: Optional[str]
    error_message: Optional[str]
    
    model_config = ConfigDict(from_attributes=True)

class EvaluationResponse(BaseModel):
    id: int
    session_id: int
    overall_score: float
    coding_score: Optional[float]
    communication_score: Optional[float]
    engagement_score: Optional[float]
    reasoning_score: Optional[float]
    recommendation: str
    confidence_level: Optional[float]
    strengths: list[str]
    weaknesses: list[str]
    key_findings: list[dict[str, Any]]
    summary: Optional[str]
    detailed_report: Optional[str]
    evaluated_at: datetime
    
    # Anomaly detection results (Phase 1)
    anomaly_probability: Optional[float] = None
    anomaly_mode: Optional[str] = None
    # BUG-06 FIX: use Field(default_factory=...) — mutable bare [] / {} are
    # shared across instances in some Pydantic v2 configurations and are
    # rejected outright in strict mode.
    anomaly_reasons: list[str] = Field(default_factory=list)
    behavioral_features: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)

class WSMessage(BaseModel):
    """Base WebSocket message."""
    type: str
    # BUG-05 FIX: datetime.utcnow() is deprecated in Python 3.12+ and
    # produces a timezone-naive datetime. Use timezone.utc explicitly.
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = Field(default_factory=dict)

class WSCodingUpdate(WSMessage):
    """Coding activity update."""
    type: str = "coding_update"

class WSMetricUpdate(WSMessage):
    """Real-time metric update."""
    type: str = "metric_update"

class WSSessionUpdate(WSMessage):
    """Session status update."""
    type: str = "session_update"
