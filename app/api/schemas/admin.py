"""Response models for the Campus Operations / admin endpoints (§10)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CaseView(BaseModel):
    case_code: str
    student_code: str
    category: str
    description: str
    priority: str
    department: str
    status: str
    created_at: datetime
    response_due_at: Optional[datetime] = None
    resolution_due_at: Optional[datetime] = None
    response_breached: Optional[bool] = None
    resolution_breached: Optional[bool] = None
