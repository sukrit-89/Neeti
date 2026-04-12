"""
Vision Agent - Analyzes visual engagement and attention.

Production hardening over the previous version:
  - Structured log entries with session_id on every path.
  - Confidence-weighted metric aggregation: a gaze sample with confidence=0.2
    should not count the same as one with confidence=0.95.
  - Null-safety on VisionMetric.label and .value before comparisons;
    the DB columns are nullable and the ORM reflects that.
  - Timeout on the AI service call to prevent the agent from hanging
    indefinitely when the AI provider is slow.
  - Explicit async context manager cleanup to prevent connection leaks
    when _generate_insights is cancelled.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from sqlalchemy import select

from app.agents.base import BaseAgent, AgentInput, AgentOutput
from app.models.models import VisionMetric
from app.core.database import AsyncSessionLocal
from app.core.logging import logger
from app.services.ai_service import ai_service

# Maximum seconds to wait for the AI service before falling back to rule-based insights.
_AI_TIMEOUT_SEC = 15


class VisionAgent(BaseAgent):
    """
    Analyzes vision metrics to assess:
      - Engagement level  (gaze sensor)
      - Attention focus   (emotion sensor)
      - Presence          (presence sensor)

    Scoring rules:
      - Only sensors that actually produced data contribute to the final score.
      - Weights are renormalised across active sensors so the score is always
        in [0, 100] regardless of which sensors fired.
      - Metric values are confidence-weighted when the model provides a
        confidence score; otherwise each sample counts equally (weight=1.0).
    """

    def get_name(self) -> str:
        return "vision"

    async def process(self, input_data: AgentInput) -> AgentOutput:
        """Process vision metrics for a session."""
        session_id = input_data.session_id

        # ── 1. Fetch metrics ─────────────────────────────────────────────────
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(VisionMetric)
                .where(VisionMetric.session_id == session_id)
                .order_by(VisionMetric.timestamp)
            )
            metrics_list = result.scalars().all()

        total_raw = len(metrics_list)
        logger.info(
            "[vision] session=%d fetched %d raw VisionMetric rows",
            session_id, total_raw,
        )

        if not metrics_list:
            logger.info("[vision] session=%d no data — returning skipped", session_id)
            return AgentOutput(
                agent_type=self.agent_type,
                session_id=session_id,
                score=None,
                status="skipped",
                findings={"counts": {"total_metrics": 0,
                                     "gaze_samples": 0,
                                     "emotion_samples": 0,
                                     "presence_samples": 0},
                          "scores": {},
                          "active_sensors": []},
                insights=(
                    "No vision data collected — "
                    "webcam analysis was not active during this session."
                ),
            )

        # ── 2. Calculate per-sensor scores ───────────────────────────────────
        metrics = self._analyze_metrics(metrics_list)

        # ── 3. Build dynamic weights (only active sensors) ───────────────────
        # Base weights reflect the relative importance of each sensor.
        # If a sensor produced no data its weight is excluded entirely and
        # the remaining weights are renormalised so they sum to 1.0.
        base_weights: dict[str, float] = {
            "engagement": 0.4,
            "attention":  0.3,
            "presence":   0.3,
        }
        sensor_sample_key: dict[str, str] = {
            "engagement": "gaze_samples",
            "attention":  "emotion_samples",
            "presence":   "presence_samples",
        }
        active_weights: dict[str, float] = {
            dim: w
            for dim, w in base_weights.items()
            if metrics[sensor_sample_key[dim]] > 0
        }

        if not active_weights:
            # metrics_list was non-empty but no row had a recognised metric_type
            logger.warning(
                "[vision] session=%d metrics present (%d rows) but no "
                "typed sensor data (gaze/emotion/presence) found.",
                session_id, total_raw,
            )
            score: Optional[float] = None
        else:
            total_w = sum(active_weights.values())
            normalised_weights = {k: v / total_w for k, v in active_weights.items()}
            # Pass only the score dimensions, never raw counts
            score_metrics = {
                k: metrics[k]
                for k in normalised_weights
                if metrics[k] is not None        # already guaranteed by active_weights filter, but be explicit
            }
            score = self.calculate_score(score_metrics, normalised_weights)
            logger.info(
                "[vision] session=%d active_sensors=%s score=%.1f",
                session_id, list(normalised_weights.keys()), score,
            )

        # ── 4. Extract flags ──────────────────────────────────────────────────
        flags = self._extract_flags(metrics)

        # ── 5. Generate insights (with timeout) ───────────────────────────────
        insights = await self._generate_insights(session_id, metrics, flags)

        return AgentOutput(
            agent_type=self.agent_type,
            session_id=session_id,
            score=score,
            findings={
                "counts": {
                    "total_metrics":    metrics["total_metrics"],
                    "gaze_samples":     metrics["gaze_samples"],
                    "emotion_samples":  metrics["emotion_samples"],
                    "presence_samples": metrics["presence_samples"],
                },
                "scores": {
                    k: metrics[k]
                    for k in ("engagement", "attention", "presence")
                    if metrics[k] is not None
                },
                "active_sensors": list(active_weights.keys()),
            },
            flags=flags,
            insights=insights,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _analyze_metrics(self, metrics_list: list[VisionMetric]) -> dict[str, Any]:
        """
        Compute per-sensor scores.

        Confidence-weighted aggregation:
          If a VisionMetric row has a non-null confidence value, that sample's
          contribution is weighted by its confidence.  Rows with confidence=None
          are treated as confidence=1.0 (equal weight).

        Returns a flat dict with:
          - total_metrics, gaze_samples, emotion_samples, presence_samples (counts)
          - engagement, attention, presence  (Optional[float], None if sensor absent)
        """
        gaze_metrics     = [m for m in metrics_list if m.metric_type == "gaze"]
        emotion_metrics  = [m for m in metrics_list if m.metric_type == "emotion"]
        presence_metrics = [m for m in metrics_list if m.metric_type == "presence"]

        engagement = self._weighted_label_score(
            samples=gaze_metrics,
            positive_labels={"focused", "looking_at_screen"},
        )
        attention = self._weighted_label_score(
            samples=emotion_metrics,
            positive_labels={"focused", "interested", "neutral"},
        )
        presence = self._weighted_value_score(presence_metrics)

        return {
            "total_metrics":    len(metrics_list),
            "gaze_samples":     len(gaze_metrics),
            "emotion_samples":  len(emotion_metrics),
            "presence_samples": len(presence_metrics),
            "engagement":       engagement,
            "attention":        attention,
            "presence":         presence,
        }

    @staticmethod
    def _weighted_label_score(
        samples: list[VisionMetric],
        positive_labels: set[str],
    ) -> Optional[float]:
        """
        Confidence-weighted fraction of samples whose label is in positive_labels.

        Edge cases:
          - Empty samples list → None (sensor absent, not zero).
          - Null label → treated as "not positive" (conservative).
          - Null confidence → weight = 1.0.
          - Total weight = 0 (all samples have confidence=0.0) → 0.0.
        """
        if not samples:
            return None

        weighted_positive = 0.0
        total_weight = 0.0

        for m in samples:
            # ISSUE: m.label is nullable in the DB — guard before membership test
            label = m.label or ""
            conf  = m.confidence if m.confidence is not None else 1.0
            # ISSUE: confidence could be negative or > 1 from a buggy sensor
            conf  = max(0.0, min(1.0, conf))

            total_weight      += conf
            if label in positive_labels:
                weighted_positive += conf

        if total_weight == 0.0:
            return 0.0

        return (weighted_positive / total_weight) * 100.0

    @staticmethod
    def _weighted_value_score(samples: list[VisionMetric]) -> Optional[float]:
        """
        Confidence-weighted fraction of presence samples where value == 1.0.

        Edge cases:
          - Empty → None.
          - Null value → treated as absent (not present).
          - Null confidence → weight = 1.0.
        """
        if not samples:
            return None

        weighted_present = 0.0
        total_weight     = 0.0

        for m in samples:
            # ISSUE: m.value is nullable in the DB
            value = m.value if m.value is not None else 0.0
            conf  = m.confidence if m.confidence is not None else 1.0
            conf  = max(0.0, min(1.0, conf))

            total_weight     += conf
            # Compare with tolerance to handle floating-point representation
            if abs(value - 1.0) < 1e-6:
                weighted_present += conf

        if total_weight == 0.0:
            return 0.0

        return (weighted_present / total_weight) * 100.0

    def _extract_flags(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract concerning patterns from computed metrics.

        Uses .get() throughout — never raises KeyError if the dict shape
        changes in a future refactor.
        """
        flags: list[dict[str, Any]] = []

        engagement = metrics.get("engagement")
        if engagement is not None and engagement < 50:
            flags.append({
                "type":     "low_engagement",
                "severity": "high",
                "message":  f"Low visual engagement detected ({engagement:.1f}%)",
                "value":    round(engagement, 1),
            })

        attention = metrics.get("attention")
        if attention is not None and attention < 50:
            flags.append({
                "type":     "low_attention",
                "severity": "medium",
                "message":  f"Low emotional attention detected ({attention:.1f}%)",
                "value":    round(attention, 1),
            })

        presence = metrics.get("presence")
        if presence is not None and presence < 80:
            flags.append({
                "type":     "intermittent_presence",
                "severity": "medium",
                "message":  f"Candidate frequently absent from camera view ({presence:.1f}%)",
                "value":    round(presence, 1),
            })

        total_metrics = metrics.get("total_metrics", 0)
        if total_metrics < 100:
            flags.append({
                "type":     "limited_vision_data",
                "severity": "low",
                "message":  f"Limited vision data collected ({total_metrics} samples — < 100 expected)",
            })

        return flags

    async def _generate_insights(
        self,
        session_id: int,
        metrics: dict[str, Any],
        flags: list[dict[str, Any]],
    ) -> str:
        """
        Generate natural language insights using AI.

        Wrapped in asyncio.wait_for to prevent hanging if the AI service
        is slow or unresponsive.  Falls back to deterministic rule-based text.
        """
        def _fmt(val: Optional[float]) -> str:
            return f"{val:.1f}/100" if val is not None else "N/A (sensor inactive)"

        prompt = (
            "Analyze this candidate's visual engagement during a technical interview:\n\n"
            f"Metrics:\n"
            f"- Total vision samples: {metrics['total_metrics']}\n"
            f"- Gaze tracking samples: {metrics['gaze_samples']}\n"
            f"- Emotion samples: {metrics['emotion_samples']}\n"
            f"- Presence samples: {metrics['presence_samples']}\n"
            f"- Engagement score: {_fmt(metrics.get('engagement'))}\n"
            f"- Attention score:  {_fmt(metrics.get('attention'))}\n"
            f"- Presence score:   {_fmt(metrics.get('presence'))}\n\n"
            f"Flags: {len(flags)} issues detected\n"
            + ("\n".join(f"- {f['message']}" for f in flags) if flags else "- None")
            + "\n\nProvide a 2-3 sentence assessment of their engagement and attentiveness. "
              "If a sensor was inactive (N/A), note that the dimension could not be assessed."
        )

        try:
            result = await asyncio.wait_for(
                ai_service.generate_completion(
                    prompt=prompt,
                    system_prompt=(
                        "You are an expert interviewer evaluating a candidate's "
                        "visual engagement. Be concise and specific."
                    ),
                    temperature=0.3,
                    max_tokens=250,
                ),
                timeout=_AI_TIMEOUT_SEC,
            )
            return result.strip()

        except asyncio.TimeoutError:
            logger.warning(
                "[vision] session=%d AI insights timed out after %ds — using fallback",
                session_id, _AI_TIMEOUT_SEC,
            )
        except Exception as exc:
            logger.warning(
                "[vision] session=%d AI insights failed: %s — using fallback",
                session_id, exc,
            )

        # ── Deterministic fallback ────────────────────────────────────────────
        return self._fallback_insights(metrics, flags)

    @staticmethod
    def _fallback_insights(
        metrics: dict[str, Any],
        flags: list[dict[str, Any]],
    ) -> str:
        """
        Rule-based insight text used when the AI service is unavailable.

        Takes flags into account so the fallback is not misleadingly positive.
        """
        engagement = metrics.get("engagement")
        presence   = metrics.get("presence")
        flag_types = {f["type"] for f in flags}

        if engagement is None and presence is None:
            return (
                "Visual engagement could not be assessed — "
                "insufficient sensor data was collected during this session."
            )

        parts: list[str] = []

        if "intermittent_presence" in flag_types:
            val = next((f["value"] for f in flags if f["type"] == "intermittent_presence"), None)
            parts.append(
                f"The candidate was absent from camera view {100 - val:.0f}% of the time." if val else
                "The candidate was frequently absent from the camera view."
            )

        if engagement is not None:
            if engagement > 80:
                parts.append("Visual engagement was high throughout the session.")
            elif engagement > 60:
                parts.append(f"Visual engagement was moderate ({engagement:.0f}%).")
            else:
                parts.append(f"Visual engagement was low ({engagement:.0f}%), suggesting distraction.")

        if not parts:
            parts.append("Insufficient data to draw reliable conclusions about visual engagement.")

        if "limited_vision_data" in flag_types:
            parts.append("Note: limited sensor data reduces confidence in this assessment.")

        return " ".join(parts)
