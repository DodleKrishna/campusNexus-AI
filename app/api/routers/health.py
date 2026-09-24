"""GET /health -- no auth. A DB connectivity check plus demo-readiness facts.

Phase 9 addition: reports which LLM provider is actually wired in (and
whether it is live or the offline mock), whether the database has been
seeded, and whether the policy store has any ingested chunks -- so the UI
and ``scripts/demo_preflight.py`` can show the true runtime mode instead
of letting mock output pass as live AI output.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.models import Student

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request, session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    student_count = session.execute(select(func.count()).select_from(Student)).scalar_one()

    try:
        chunks = request.app.state.knowledge_service.indexed_chunk_count()
        policy_store = {"available": chunks > 0, "chunks": chunks}
    except Exception as exc:  # noqa: BLE001 -- readiness probe: report, never crash the health check
        policy_store = {"available": False, "chunks": 0, "error": f"{type(exc).__name__}: {exc}"}

    provider = request.app.state.llm_provider
    return {
        "status": "ok",
        "service": "campusnexus-api",
        "ready": student_count > 0 and policy_store["available"],
        "database": {"seeded": student_count > 0, "students": student_count},
        "policy_store": policy_store,
        "llm": {"provider": provider.name, "live": provider.is_live, "model": provider.model_name},
        # Phase 14: whether a live provider *could* be used (a key is present in
        # the API's environment). Never the key itself. The running mode is
        # still only what ``llm`` says -- nothing switches automatically.
        "live_ai_configured": {"groq": bool(os.environ.get("GROQ_API_KEY")), "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY"))},
    }
