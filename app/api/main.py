"""FastAPI application factory (Phase 8).

``create_app`` wires the same shared backend singletons every demo
script/eval runner already builds (session_factory, MissionOrchestrator,
ToolGateway, KnowledgeService) onto a thin HTTP layer -- nothing here
reimplements agent/rule/verification logic; every route composes existing
``app.services``/``app.db.repositories``/``app.graph``/``app.tools`` calls.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncContextManager, AsyncIterator, Callable

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.agents.academic.agent import AcademicAgent
from app.agents.action.agent import ActionAgent
from app.agents.career.agent import CareerAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.api.routers import admin, agents, approvals, auth, health, me, missions, students
from app.db.session import create_db_engine, create_session_factory, upgrade_schema
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.schemas.enums import AgentName
from app.services.agent_chat import SpecialistGateway
from app.services.knowledge import KnowledgeService
from app.tools.build import build_default_tool_registry
from app.tools.registry import ToolGateway


def _build_registry(knowledge_service: KnowledgeService, llm_provider: LLMProvider, tool_gateway: ToolGateway) -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: AcademicAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider))
    registry.register(AgentName.CAREER_AGENT, lambda s: CareerAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider))
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm_provider))
    registry.register(AgentName.ACTION_AGENT, lambda s: ActionAgent(session=s, knowledge_service=knowledge_service, tool_gateway=tool_gateway))
    return registry


def create_app(
    *,
    session_factory: sessionmaker[Session],
    knowledge_service: KnowledgeService,
    llm_provider: LLMProvider,
    tool_gateway: ToolGateway | None = None,
    lifespan: Callable[[FastAPI], AsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Build a fully-wired FastAPI app.

    Tests pass an isolated ``session_factory`` (a temp DB) and a fresh
    ``MockLLMProvider``; the module-level ``app`` below passes the real
    configured ones. This factory pattern is what lets tests exercise the
    real routers/dependencies against isolated state instead of the shared dev DB.
    """
    tool_gateway = tool_gateway or build_default_tool_registry()
    registry = _build_registry(knowledge_service, llm_provider, tool_gateway)
    orchestrator = MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm_provider)

    fastapi_app = FastAPI(
        title="CampusNexus AI API",
        description=(
            "Local demo API exposing the CampusNexus multi-agent backend. "
            "No production authentication -- see docs/ARCHITECTURE.md's Phase 8 trust-boundary note."
        ),
        version="0.8.0",
        lifespan=lifespan,
    )
    fastapi_app.state.session_factory = session_factory
    fastapi_app.state.orchestrator = orchestrator
    fastapi_app.state.tool_gateway = tool_gateway
    fastapi_app.state.knowledge_service = knowledge_service
    fastapi_app.state.llm_provider = llm_provider
    fastapi_app.state.specialist_gateway = SpecialistGateway(registry=registry, session_factory=session_factory)

    fastapi_app.include_router(health.router)
    fastapi_app.include_router(students.router)
    fastapi_app.include_router(missions.router)
    fastapi_app.include_router(approvals.router)
    fastapi_app.include_router(admin.router)
    # Phase 15: JWT-authenticated React application API.
    fastapi_app.include_router(auth.router)
    fastapi_app.include_router(me.router)
    fastapi_app.include_router(agents.router)
    return fastapi_app


def _build_default_app() -> FastAPI:
    engine = create_db_engine()
    session_factory = create_session_factory(engine)

    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider)
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm_provider = get_llm_provider(None)  # CAMPUSNEXUS_LLM_PROVIDER env var, defaults to "mock"

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # At server startup, never at import time (tests import this module and
        # must never touch the dev DB): additively brings a database created by
        # an earlier phase up to the current columns (e.g. Phase 11 approval
        # binding) without touching data.
        upgrade_schema(engine)
        yield

    return create_app(
        session_factory=session_factory, knowledge_service=knowledge_service, llm_provider=llm_provider, lifespan=lifespan
    )


app = _build_default_app()
