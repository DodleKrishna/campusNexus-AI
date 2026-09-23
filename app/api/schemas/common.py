"""Small API-layer types shared across routers."""
from __future__ import annotations

from pydantic import BaseModel


class DemoIdentityView(BaseModel):
    key: str
    role: str
    display_name: str
    student_id: str | None = None
