"""Cross-agent Orchestrator integration tests (Phase 6): dependency-aware
information sharing, missing-upstream-result safety, and full multi-agent
mission completion, using the *real* Academic/Career/Events/Campus Services
agents (not test doubles) against the real seeded DB + real RAG fixtures and
the MockLLMProvider -- no live LLM, no network.
"""
from __future__ import annotations

from app.agents.academic.agent import AcademicAgent
from app.agents.career.agent import CareerAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.providers.mock import MockLLMProvider
from app.schemas.enums import AgentName, MissionStatus, TaskStatus, UserRole

DEMO_STUDENT = "STU-DEMO-001"

FLAGSHIP_GOAL = (
    "I'm a third-year CSE student interested in AI. Find internships I'm eligible for, identify my skill "
    "gaps, find relevant workshops that don't conflict with my classes, and create a preparation plan."
)
CAMPUS_SERVICES_GOAL = (
    "Check the status of my campus complaints, identify any overdue cases and explain the applicable "
    "grievance procedures."
)


def _full_registry(session_factory_unused, knowledge_service, llm) -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: AcademicAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAREER_AGENT, lambda s: CareerAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=knowledge_service, llm_provider=llm))
    return registry


def _orchestrator(session_factory, knowledge_service, max_replans=2) -> MissionOrchestrator:
    llm = MockLLMProvider()
    registry = _full_registry(session_factory, knowledge_service, llm)
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm, max_replans=max_replans)


def _task_ids(final_state) -> dict:
    """Maps task index (1-based, by declared plan order) to task_id."""
    return {i + 1: task.task_id for i, task in enumerate(final_state["plan"].tasks)}


def test_flagship_mission_routes_each_task_to_the_correct_agent(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _orchestrator(session_factory, knowledge_service)
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)

    assert final["mission_status"] == MissionStatus.COMPLETED
    plan_agents = {task.task_id: task.agent for task in final["plan"].tasks}
    agents_used = {task_id: result.agent for task_id, result in final["agent_results"].items()}
    assert agents_used == plan_agents
    assert set(plan_agents.values()) == {AgentName.ACADEMIC_AGENT, AgentName.CAREER_AGENT, AgentName.EVENTS_OPPORTUNITY_AGENT}


def test_flagship_mission_respects_dependency_ordering(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _orchestrator(session_factory, knowledge_service)
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)

    events_task = next(t for t in final["plan"].tasks if t.agent == AgentName.EVENTS_OPPORTUNITY_AGENT)
    assert len(events_task.dependencies) == 3
    assert all(final["task_status"][dep] == TaskStatus.COMPLETED for dep in events_task.dependencies)


def test_flagship_mission_propagates_skill_gaps_from_career_to_events(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _orchestrator(session_factory, knowledge_service)
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)

    career_task = next(t for t in final["plan"].tasks if t.agent == AgentName.CAREER_AGENT)
    events_task = next(t for t in final["plan"].tasks if t.agent == AgentName.EVENTS_OPPORTUNITY_AGENT)

    skill_gaps = set(final["agent_results"][career_task.task_id].facts["skill_gaps"])
    assert skill_gaps  # Career genuinely found gaps

    events_assessments = final["agent_results"][events_task.task_id].facts["assessments"]
    assert events_assessments
    ai_workshop = next(a for a in events_assessments if "Artificial Intelligence" in a["event"]["title"])
    assert ai_workshop["conflict_check_performed"] is True
    assert ai_workshop["timetable_conflicts"] == []
    assert ai_workshop["exam_conflicts"] == []
    assert ai_workshop["already_registered"] is True


def test_final_result_is_synthesized_from_genuine_multi_agent_outputs_not_hardcoded(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _orchestrator(session_factory, knowledge_service)
    final = orchestrator.run_mission(FLAGSHIP_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)

    result_text = final["final_result"]
    assert "Operating Systems" in result_text  # from the real Academic timetable
    assert "AI Software Engineering Intern" in result_text  # from the real Career eligibility check
    assert "Artificial Intelligence & Deep Learning Workshop" in result_text  # from the real Events discovery


def test_missing_upstream_result_blocks_the_dependent_task(seeded_session, session_factory, knowledge_service) -> None:
    """If the Career task fails (unknown student), the dependent Events task
    must never run -- it cannot fabricate the skill_gaps it never received."""
    orchestrator = _orchestrator(session_factory, knowledge_service, max_replans=0)
    final = orchestrator.run_mission(
        FLAGSHIP_GOAL, user_id="STU-NOPE-999", user_role=UserRole.STUDENT, student_id="STU-NOPE-999"
    )

    career_task = next(t for t in final["plan"].tasks if t.agent == AgentName.CAREER_AGENT)
    events_task = next(t for t in final["plan"].tasks if t.agent == AgentName.EVENTS_OPPORTUNITY_AGENT)

    assert final["task_status"][career_task.task_id] == TaskStatus.FAILED
    assert final["task_status"][events_task.task_id] == TaskStatus.SKIPPED
    assert events_task.task_id not in final["agent_results"]  # never dispatched, never ran
    assert final["mission_status"] == MissionStatus.FAILED


def test_campus_services_mission_completes_independently(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _orchestrator(session_factory, knowledge_service)
    final = orchestrator.run_mission(CAMPUS_SERVICES_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert len(final["plan"].tasks) == 1
    assert final["plan"].tasks[0].agent == AgentName.CAMPUS_SERVICES_AGENT

    result_text = final["final_result"]
    assert "CASE-0001" in result_text
    assert "CASE-0002" in result_text
    assert "CASE-0003" in result_text
    assert "Overdue" in result_text


def test_campus_services_mission_does_not_auto_escalate(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _orchestrator(session_factory, knowledge_service)
    final = orchestrator.run_mission(CAMPUS_SERVICES_GOAL, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)
    services_task = final["plan"].tasks[0]
    result = final["agent_results"][services_task.task_id]
    # Read-only: no proposed_actions (the Action Agent doesn't exist yet, and
    # this phase never writes/escalates anything).
    assert result.proposed_actions == []


def test_existing_academic_only_mission_is_unaffected_by_new_agents(seeded_session, session_factory, knowledge_service) -> None:
    """Backward compatibility: registering three more agents must not change
    behavior for a plain academic-only goal (generic clause-splitting fallback)."""
    orchestrator = _orchestrator(session_factory, knowledge_service)
    final = orchestrator.run_mission("Can I write my OS exam?", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert len(final["plan"].tasks) == 1
    assert final["plan"].tasks[0].agent == AgentName.ACADEMIC_AGENT
    assert "68.0%" in final["final_result"]
