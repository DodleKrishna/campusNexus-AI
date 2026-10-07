"""GET /health -- no auth. A DB connectivity check plus demo-readiness facts.

Phase 9 addition: reports which LLM provider is actually wired in (and
whether it is live or the offline mock), whether the database has been
seeded, and whether the policy store has any ingested chunks -- so the UI
and ``scripts/demo_preflight.py`` can show the true runtime mode instead
of letting mock output pass as live AI output.

Phase 21: ``database`` also reports ``ready`` and the ``dialect``
(``sqlite``/``postgresql``) -- never the host, user, password or URL. A
database that cannot be reached answers ``ready: false`` instead of a 500.
Phase 2.5 adds ``status`` (``connected``/``unavailable``) and ``type``.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request

from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.db.readiness import database_readiness
from app.db.session import describe_database

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request, session: Session = Depends(get_session)) -> dict:
    engine = session.get_bind()
    backend = describe_database(engine)
    try:
        # Phase 22B: unauthenticated, so no organization: an engine-level aggregate, never tenant rows.
        database_ready, student_count = database_readiness(engine)
    except Exception:  # noqa: BLE001 -- readiness probe; the error text may name the host, so it is not echoed
        student_count, database_ready = 0, False

    try:
        chunks = request.app.state.knowledge_service.indexed_chunk_count()
        policy_store = {"available": chunks > 0, "chunks": chunks}
    except Exception as exc:  # noqa: BLE001 -- readiness probe: report, never crash the health check
        policy_store = {"available": False, "chunks": 0, "error": f"{type(exc).__name__}: {exc}"}

    provider = request.app.state.llm_provider
    return {
        "status": "ok",
        "service": "campusnexus-api",
        "ready": database_ready and student_count > 0 and policy_store["available"],
        "database": {
            # Phase 2.5: connected|unavailable and sqlite|postgresql only -- never a host, URL, user or project ref.
            "status": "connected" if database_ready else "unavailable", "type": backend["dialect"],
            "ready": database_ready, "dialect": backend["dialect"], "label": backend["label"],
            "seeded": student_count > 0, "students": student_count,
        },
        "policy_store": policy_store,
        "llm": {"provider": provider.name, "live": provider.is_live, "model": provider.model_name},
        # Phase 14: whether a live provider *could* be used (a key is present in
        # the API's environment). Never the key itself. The running mode is
        # still only what ``llm`` says -- nothing switches automatically.
        "live_ai_configured": {"groq": bool(os.environ.get("GROQ_API_KEY")), "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY"))},
    }
