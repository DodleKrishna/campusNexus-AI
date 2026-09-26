"""Agent-as-a-Product MVP: an organization's deployment of one catalog agent.

Tenant-owned (``TenantMixin``): the organization comes from the tenant session,
never from the client. One deployment per (organization, agent_key). Only
product agents from ``app.services.agent_catalog.PRODUCT_AGENTS`` can be
deployed; the Action Agent, Verifier and Approval Gate never can.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import JSON, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UTCDateTime, portable_enum, utc_now
from app.schemas.enums import IntelligenceLevel

DEPLOYMENT_ACTIVE, DEPLOYMENT_PAUSED = "active", "paused"


class AgentDeployment(TenantMixin, Base):
    __tablename__ = "agent_deployments"
    __table_args__ = (Index("ux_agent_deployments_org_agent", "organization_id", "agent_key", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(40))
    display_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default=DEPLOYMENT_ACTIVE)
    intelligence_level: Mapped[IntelligenceLevel] = mapped_column(portable_enum(IntelligenceLevel))
    monthly_budget_usd: Mapped[Optional[float]] = mapped_column(Float, default=None)  # shown; the org budget enforces
    requires_approval: Mapped[bool] = mapped_column(default=False)
    allowed_roles: Mapped[List[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
