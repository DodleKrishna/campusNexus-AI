"""FastAPI application factory (Phase 8).

``create_app`` wires the same shared backend singletons every demo
script/eval runner already builds (session_factory, MissionOrchestrator,
ToolGateway, KnowledgeService) onto a thin HTTP layer -- nothing here
reimplements agent/rule/verification logic; every route composes existing
``app.services``/``app.db.repositories``/``app.graph``/``app.tools`` calls.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncContextManager, AsyncIterator, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from app.agents.academic.agent import AcademicAgent
from app.agents.action.agent import ActionAgent
from app.agents.career.agent import CareerAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.agentos.brain import AgentBrain
from app.agentos.bootstrap import build_agent_runtime
from app.agentos.providers import build_agent_brain
from app.communication.voice.conversation import voice_pipeline_from_env
from app.api.routers import admin, admin_console, agentos, agents, assignments, approvals, attendance, auth, enterprise, exams, faculty, health, hod, me, missions, requests, students, voice
from app.db.session import create_db_engine, upgrade_schema, verify_database_ready
from app.db.tenant_session import TenantSessionFactory
from app.llm.router import build_routed_provider, routed
from app.services.ai_usage import AIUsageRecorder
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.base import LLMProvider
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


_DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173")


def _allowed_origins() -> list[str]:
    configured = [o.strip().rstrip("/") for o in os.environ.get("CAMPUSNEXUS_FRONTEND_ORIGIN", "").split(",") if o.strip()]
    return [*_DEV_ORIGINS, *configured]


def create_app(
    *,
    session_factory: "sessionmaker[Session] | TenantSessionFactory",
    knowledge_service: KnowledgeService,
    llm_provider: LLMProvider,
    tool_gateway: ToolGateway | None = None,
    lifespan: Callable[[FastAPI], AsyncContextManager[None]] | None = None,
    clock: Callable[[], datetime] | None = None,
    agent_brain: AgentBrain | None = None,
) -> FastAPI:
    """Build a fully-wired FastAPI app.

    Tests pass an isolated ``session_factory`` (a temp DB) and a fresh
    ``MockLLMProvider``; the module-level ``app`` below passes the real
    configured ones. This factory pattern is what lets tests exercise the
    real routers/dependencies against isolated state instead of the shared dev DB.

    Phase 22B: whatever factory is passed, the application only ever uses tenant sessions
    (``TenantSessionFactory``): request sessions are bound by the caller's identity, and the
    orchestrator and specialist gateway open sessions bound to the mission's / caller's organization.
    """
    session_factory = TenantSessionFactory.from_sessionmaker(session_factory)
    # Every AI call is routed (NO-AI / 20B / 120B) and metered per organization, before it runs.
    llm_provider = routed(llm_provider, recorder=AIUsageRecorder(session_factory))
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
    # The dev frontend reaches the API through the Vite proxy (same origin); a hosted frontend (e.g. Netlify)
    # calls it cross-origin. Allowed origins: local dev plus CAMPUSNEXUS_FRONTEND_ORIGIN (comma-separated).
    # Bearer tokens travel in the Authorization header, so no cookies / credentials mode is needed.
    fastapi_app.add_middleware(
        CORSMiddleware, allow_origins=_allowed_origins(), allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Demo-Identity"],
    )
    fastapi_app.state.session_factory = session_factory
    fastapi_app.state.orchestrator = orchestrator
    fastapi_app.state.tool_gateway = tool_gateway
    fastapi_app.state.knowledge_service = knowledge_service
    fastapi_app.state.llm_provider = llm_provider
    fastapi_app.state.specialist_gateway = SpecialistGateway(registry=registry, session_factory=session_factory)
    fastapi_app.state.clock = clock or (lambda: datetime.now(timezone.utc))
    # AgentOS V2 kernel: Nexus (Phase 2, read-only tools) and the Assignment Guardian (Phase 3, created by publishing,
    # advanced by scripts/process_due_missions.py). The brain comes from CAMPUSNEXUS_AGENT_BRAIN (default: offline
    # mock); an unusable configured provider refuses explicitly.
    brain = agent_brain if agent_brain is not None else build_agent_brain(recorder=llm_provider.recorder)
    fastapi_app.state.agent_runtime = build_agent_runtime(brain, clock=lambda: fastapi_app.state.clock())
    # Phase 5.1: the live voice pipeline (Groq Whisper STT -> restricted voice brain -> Groq Orpheus TTS), from the env.
    # Unavailable (no GROQ_API_KEY / TTS voice) = voice streams are refused with the reason; never fake speech.
    fastapi_app.state.voice_pipeline, fastapi_app.state.voice_pipeline_unavailable = voice_pipeline_from_env()

    fastapi_app.include_router(health.router)
    fastapi_app.include_router(students.router)
    fastapi_app.include_router(missions.router)
    fastapi_app.include_router(approvals.router)
    fastapi_app.include_router(admin.router)
    # Phase 15: JWT-authenticated React application API.
    fastapi_app.include_router(auth.router)
    fastapi_app.include_router(me.router)
    fastapi_app.include_router(agents.router)
    # Phase 16: faculty operations, live attendance and workflow requests.
    fastapi_app.include_router(faculty.router)
    fastapi_app.include_router(requests.router)
    # Phase 17: department operations for the head of department.
    fastapi_app.include_router(hod.router)
    # Phase 18: the administrator console (JWT, institution-wide).
    fastapi_app.include_router(admin_console.router)
    fastapi_app.include_router(enterprise.router)
    fastapi_app.include_router(agentos.router)
    fastapi_app.include_router(agentos.assistant_router)
    fastapi_app.include_router(assignments.router)
    fastapi_app.include_router(exams.router)
    fastapi_app.include_router(attendance.router)
    fastapi_app.include_router(voice.router)  # Phase 5: provider callbacks + voice stream
    return fastapi_app


def _build_default_app() -> FastAPI:
    engine = create_db_engine()
    session_factory = TenantSessionFactory(engine)

    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider)
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm_provider = build_routed_provider(None)  # CAMPUSNEXUS_LLM_PROVIDER, defaults to "mock"; Groq = 20B + 120B

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # At server startup, never at import time (tests import this module and
        # must never touch the dev DB): additively brings a database created by
        # an earlier phase up to the current columns (e.g. Phase 11 approval
        # binding) without touching data.
        if os.environ.get("CAMPUSNEXUS_DEMO_BOOTSTRAP", "").strip() == "1":
            # Hosted demo (ephemeral SQLite): seed once at startup if empty -- never per request, never a reset.
            from scripts.render_bootstrap import bootstrap_demo

            bootstrap_demo(engine, vector_store, config.policy_dir)
        if engine.dialect.name == "postgresql":
            # Production PostgreSQL: fail startup visibly if unreachable or behind; never alter its schema here
            # (python scripts/upgrade_database.py applies the additive upgrade deliberately).
            verify_database_ready(engine)
        else:
            upgrade_schema(engine)
        yield

    return create_app(
        session_factory=session_factory, knowledge_service=knowledge_service, llm_provider=llm_provider, lifespan=lifespan
    )


app = _build_default_app()
