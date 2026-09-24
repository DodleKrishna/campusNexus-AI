"""Deterministic multi-agent evaluation (no live LLM, no network).

Mirrors eval/orchestration_scenarios.py's PASS/FAIL structure, but runs the
*real* Academic/Career/Events/Campus Services agents (not test doubles)
against the real seeded SQLite DB and real Chroma policy store, with
MockLLMProvider so results are fully deterministic. Every check below reads
*structured* facts (agent assignments, eligibility/conflict/SLA booleans,
document ids) -- never a substring match against the free-text final
response -- per the Phase 6 spec's "do not evaluate merely whether the final
response contains expected keywords."

Usage:
    python eval/multiagent_scenarios.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.agents.academic.agent import AcademicAgent
from app.agents.career.agent import CareerAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.db.session import open_database, create_session_factory
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.providers.mock import MockLLMProvider
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.schemas.enums import AgentName, MissionStatus, TaskStatus, UserRole
from app.services.knowledge import KnowledgeService

DEMO_STUDENT = "STU-DEMO-001"
FLAGSHIP_GOAL = (
    "I'm a third-year CSE student interested in AI. Find internships I'm eligible for, identify my skill "
    "gaps, find relevant workshops that don't conflict with my classes, and create a preparation plan."
)
CAMPUS_SERVICES_GOAL = (
    "Check the status of my campus complaints, identify any overdue cases and explain the applicable "
    "grievance procedures."
)


def build_orchestrator() -> MissionOrchestrator:
    engine = open_database()
    session_factory = create_session_factory(engine)

    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider
    )
    retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
    knowledge_service = KnowledgeService(retriever=retriever)

    llm = MockLLMProvider()
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: AcademicAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAREER_AGENT, lambda s: CareerAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))

    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm)


def scenario_agent_routing(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    agents_by_task = {t.task_id: t.agent for t in final["plan"].tasks}
    expected_agent_set = {AgentName.ACADEMIC_AGENT, AgentName.CAREER_AGENT, AgentName.EVENTS_OPPORTUNITY_AGENT}
    passed = (
        final["mission_status"] == MissionStatus.COMPLETED
        and set(agents_by_task.values()) == expected_agent_set
        and all(final["agent_results"][tid].agent == agent for tid, agent in agents_by_task.items())
    )
    return {"passed": passed, "agents_by_task": {k: v.value for k, v in agents_by_task.items()}}


def scenario_dependency_ordering(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    events_task = next(t for t in final["plan"].tasks if t.agent == AgentName.EVENTS_OPPORTUNITY_AGENT)
    passed = len(events_task.dependencies) == 3 and all(
        final["task_status"][dep] == TaskStatus.COMPLETED for dep in events_task.dependencies
    )
    return {"passed": passed, "events_task_dependencies": events_task.dependencies}


def scenario_eligibility_correctness(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    career_task = next(t for t in final["plan"].tasks if t.agent == AgentName.CAREER_AGENT)
    eligibilities = {e["opportunity"]["title"]: e for e in final["agent_results"][career_task.task_id].facts["eligibilities"]}
    checks = [
        eligibilities["AI Software Engineering Intern"]["status"] == "eligible",
        eligibilities["Quant Research Intern"]["status"] == "not_eligible",
        eligibilities["Embedded Systems Intern"]["status"] == "not_eligible",
        eligibilities["Cloud DevOps Intern"]["status"] == "not_eligible",
        "Data Analyst Intern" not in eligibilities,  # expired -> excluded entirely
    ]
    return {"passed": all(checks), "checks": checks}


def scenario_conflict_correctness(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    events_task = next(t for t in final["plan"].tasks if t.agent == AgentName.EVENTS_OPPORTUNITY_AGENT)
    assessments = {a["event"]["title"]: a for a in final["agent_results"][events_task.task_id].facts["assessments"]}
    ai_workshop = assessments.get("Artificial Intelligence & Deep Learning Workshop")
    passed = (
        ai_workshop is not None
        and ai_workshop["conflict_check_performed"] is True
        and ai_workshop["timetable_conflicts"] == []
        and ai_workshop["exam_conflicts"] == []
        and ai_workshop["already_registered"] is True
    )
    return {"passed": passed, "ai_workshop_assessment": ai_workshop}


def scenario_evidence_propagation(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    all_evidence = [ev for result in final["agent_results"].values() for ev in result.evidence]
    passed = bool(all_evidence) and all(ev.document_id != "internal-case-escalation-sop" for ev in all_evidence)
    return {"passed": passed, "evidence_document_ids": sorted({ev.document_id for ev in all_evidence})}


def scenario_verification_status(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    statuses = {tid: v.status.value for tid, v in final["verifications"].items()}
    passed = final["mission_status"] == MissionStatus.COMPLETED and all(s == "verified" for s in statuses.values())
    return {"passed": passed, "verification_statuses": statuses}


def scenario_campus_services_mission(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(CAMPUS_SERVICES_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    task = final["plan"].tasks[0]
    assessments = {a["case"]["case_code"]: a for a in final["agent_results"][task.task_id].facts["case_assessments"]}
    checks = [
        final["mission_status"] == MissionStatus.COMPLETED,
        task.agent == AgentName.CAMPUS_SERVICES_AGENT,
        assessments["CASE-0001"]["response_breached"] is True,
        assessments["CASE-0001"]["resolution_breached"] is False,
        assessments["CASE-0002"]["response_breached"] is False,
        assessments["CASE-0002"]["resolution_breached"] is True,
        assessments["CASE-0003"]["response_breached"] is True,
        assessments["CASE-0003"]["resolution_breached"] is True,
        final["agent_results"][task.task_id].proposed_actions == [],  # never auto-escalates
    ]
    return {"passed": all(checks), "checks": checks}


def scenario_missing_upstream_blocks_dependent(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id="STU-NOPE-999", user_role=UserRole.STUDENT, student_id="STU-NOPE-999")
    career_task = next(t for t in final["plan"].tasks if t.agent == AgentName.CAREER_AGENT)
    events_task = next(t for t in final["plan"].tasks if t.agent == AgentName.EVENTS_OPPORTUNITY_AGENT)
    passed = (
        final["task_status"][career_task.task_id] == TaskStatus.FAILED
        and final["task_status"][events_task.task_id] == TaskStatus.SKIPPED
        and events_task.task_id not in final["agent_results"]
    )
    return {"passed": passed, "career_status": final["task_status"][career_task.task_id].value, "events_status": final["task_status"][events_task.task_id].value}


def scenario_backward_compatible_academic_mission(orchestrator: MissionOrchestrator) -> Dict[str, Any]:
    final = orchestrator.run_mission("Can I write my OS exam?", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    passed = (
        final["mission_status"] == MissionStatus.COMPLETED
        and len(final["plan"].tasks) == 1
        and final["plan"].tasks[0].agent == AgentName.ACADEMIC_AGENT
    )
    return {"passed": passed, "task_count": len(final["plan"].tasks)}


SCENARIOS = [
    ("agent-routing", scenario_agent_routing),
    ("dependency-ordering", scenario_dependency_ordering),
    ("eligibility-correctness", scenario_eligibility_correctness),
    ("conflict-correctness", scenario_conflict_correctness),
    ("evidence-propagation", scenario_evidence_propagation),
    ("verification-status", scenario_verification_status),
    ("campus-services-mission", scenario_campus_services_mission),
    ("missing-upstream-blocks-dependent", scenario_missing_upstream_blocks_dependent),
    ("backward-compatible-academic-mission", scenario_backward_compatible_academic_mission),
]


def main() -> int:
    orchestrator = build_orchestrator()
    failures = 0
    for name, scenario_fn in SCENARIOS:
        try:
            result = scenario_fn(orchestrator)
        except Exception as exc:  # noqa: BLE001 -- a scenario raising is itself a FAIL, not a crash of the runner
            result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        status = "PASS" if result.get("passed") else "FAIL"
        if not result.get("passed"):
            failures += 1
        print(f"{status} {name}")
        for key, value in result.items():
            if key != "passed":
                print(f"     {key}: {value}")

    total = len(SCENARIOS)
    print(f"\n{total - failures}/{total} scenarios passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
