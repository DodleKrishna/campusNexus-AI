"""Enterprise admin surfaces (JWT, ADMIN): Command Center, Workflows, Proactive Monitors, Knowledge, Connectors.

Read-only aggregations (plus the audited "Run check" of a monitor) over the caller's
organization -- the organization always comes from the token, never the client.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_knowledge_service, get_now, get_session
from app.api.routers.admin_console import current_admin
from app.db.models.auth import AuthAccount
from app.services import enterprise
from app.services.knowledge import KnowledgeService

router = APIRouter(prefix="/admin", tags=["enterprise"])


@router.get("/command-center", response_model=enterprise.CommandCenter)
def command_center(admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
                   knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now)) -> enterprise.CommandCenter:
    return enterprise.command_center(session, knowledge, admin, now)


@router.get("/workflows", response_model=List[enterprise.WorkflowCard])
def workflows(admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
              now: datetime = Depends(get_now)) -> List[enterprise.WorkflowCard]:
    return enterprise.workflows(session, admin, now)


@router.get("/monitors", response_model=List[enterprise.Monitor])
def monitors(admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
             knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now)) -> List[enterprise.Monitor]:
    return enterprise.monitors(session, knowledge, admin, now)


@router.post("/monitors/{key}/run", response_model=enterprise.Monitor)
def run_monitor(key: str, admin: AuthAccount = Depends(current_admin), session: Session = Depends(get_session),
                knowledge: KnowledgeService = Depends(get_knowledge_service), now: datetime = Depends(get_now)) -> enterprise.Monitor:
    try:
        return enterprise.run_monitor(session, knowledge, admin, key, now)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"There is no '{key}' monitor.") from exc


@router.get("/knowledge", response_model=enterprise.KnowledgeHub)
def knowledge_hub(_: AuthAccount = Depends(current_admin), knowledge: KnowledgeService = Depends(get_knowledge_service)) -> enterprise.KnowledgeHub:
    return enterprise.knowledge_hub(knowledge)


@router.get("/connectors", response_model=List[enterprise.Connector])
def connectors(_: AuthAccount = Depends(current_admin), session: Session = Depends(get_session)) -> List[enterprise.Connector]:
    return enterprise.connectors(session)
