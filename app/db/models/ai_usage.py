"""AI usage telemetry (hackathon fast-finish): one row per routed AI call or NO-AI resolution.

Tokens and cost are nullable on purpose: when a provider does not report token
usage (or no model was called) they stay NULL -- "unavailable", never invented.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UTCDateTime, portable_enum, utc_now
from app.schemas.enums import IntelligenceLevel


class AIUsageEvent(TenantMixin, Base):
    __tablename__ = "ai_usage_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(60))
    level: Mapped[IntelligenceLevel] = mapped_column(portable_enum(IntelligenceLevel))
    model: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    provider: Mapped[Optional[str]] = mapped_column(String(30), default=None)
    mission_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, default=None)
    agent_key: Mapped[Optional[str]] = mapped_column(String(40), index=True, default=None)  # deployed agent
    run_id: Mapped[Optional[str]] = mapped_column(String(40), index=True, default=None)  # one agent run
    input_tokens: Mapped[Optional[int]] = mapped_column(default=None)
    output_tokens: Mapped[Optional[int]] = mapped_column(default=None)
    latency_ms: Mapped[Optional[int]] = mapped_column(default=None)
    success: Mapped[bool] = mapped_column(default=True)
    error_kind: Mapped[Optional[str]] = mapped_column(String(60), default=None)
    estimated_cost_usd: Mapped[Optional[float]] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, index=True)
