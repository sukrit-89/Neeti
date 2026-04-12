"""
Base agent interface and abstract classes.
All AI agents inherit from these base classes.
"""
from abc import ABC, abstractmethod
from typing import Any, Optional
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.core.logging import logger


class AgentInput(BaseModel):
    """Base input for all agents."""
    session_id: int
    data: dict[str, Any]
    # BUG-06 class: mutable bare {} default is rejected in Pydantic v2 strict mode
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    """Base output from all agents."""
    agent_type: str
    session_id: int
    score: Optional[float] = None
    # BUG-06 class: mutable bare {} / [] defaults
    findings: dict[str, Any] = Field(default_factory=dict)
    flags: list[dict[str, Any]] = Field(default_factory=list)
    insights: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: str = "completed"
    error_message: Optional[str] = None


class BaseAgent(ABC):
    """
    Abstract base class for all AI agents.
    
    Each agent must implement:
    - process(): Main processing logic
    - get_name(): Agent identifier
    """
    
    def __init__(self):
        self.agent_type = self.get_name()
    
    @abstractmethod
    async def process(self, input_data: AgentInput) -> AgentOutput:
        """
        Process input and return analysis output.
        
        Args:
            input_data: Agent-specific input data
        
        Returns:
            AgentOutput with analysis results
        """
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Return agent type identifier."""
        pass
    
    async def run(self, input_data: AgentInput) -> AgentOutput:
        """
        Execute agent with error handling and logging.
        """
        # BUG-05 class: datetime.utcnow() is deprecated in Python 3.12+
        # and produces a timezone-naive datetime.
        started_at = datetime.now(timezone.utc)
        
        try:
            logger.info(
                f"{self.agent_type} agent started processing session {input_data.session_id}"
            )
            
            output = await self.process(input_data)
            output.started_at = started_at
            output.completed_at = datetime.now(timezone.utc)
            # Preserve agent-set status (e.g. "skipped" when no data)
            if output.status == "completed" or output.status == "":
                output.status = "completed"
            
            logger.info(
                f"{self.agent_type} agent completed session {input_data.session_id} "
                f"with score {output.score}"
            )
            
            return output
            
        except Exception as e:
            logger.error(
                f"{self.agent_type} agent failed for session {input_data.session_id}: {e}"
            )
            
            return AgentOutput(
                agent_type=self.agent_type,
                session_id=input_data.session_id,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                status="failed",
                error_message=str(e)
            )
    
    def calculate_score(self, metrics: dict[str, float], weights: dict[str, float]) -> float:
        """
        Helper to calculate weighted score from metrics.

        Iterates over ``weights`` (not ``metrics``) so that any extra keys in
        the metrics dict (e.g. raw sample counts) can never accidentally affect
        the score, even if a future developer adds a matching weight entry.

        Args:
            metrics: Dict of *scoring* metric names to values in [0, 100].
                     Pass only the keys you intend to score — never raw counts.
            weights: Dict of metric name to weight. Values should sum to 1.0;
                     if they don't, the result is still normalised correctly.

        Returns:
            Weighted score clamped to [0.0, 100.0]
        """
        total_score = 0.0
        total_weight = 0.0

        # FIX BUG-02: drive the loop from weights, not metrics.
        # This guarantees only intentionally-weighted keys contribute.
        for metric, weight in weights.items():
            value = metrics.get(metric)
            if value is None or weight == 0.0:
                continue
            total_score += value * weight
            total_weight += weight

        if total_weight == 0:
            return 0.0

        return min(100.0, max(0.0, total_score / total_weight))
