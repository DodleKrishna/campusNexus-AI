"""End-to-end tests for the EventsAgent (real seeded DB + real RAG fixtures,
MockLLMProvider -- no live LLM, no network).
"""
from __future__ import annotations

from app.agents.events.agent import EventsAgent
from app.llm.providers.mock import MockLLMProvider
from app.schemas.agent import AgentMessage
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus
from app.services import academic as academic_service

DEMO_STUDENT = "STU-DEMO-001"


def _message(student_id: str, query: str, *, extra_facts: dict | None = None) -> AgentMessage:
    facts = {"student_id": student_id, "query": query}
    facts.update(extra_facts or {})
    return AgentMessage(
        message_id="msg-1", mission_id="mission-1", task_id="task-1",
        source=AgentName.MISSION_ORCHESTRATOR, target=AgentName.EVENTS_OPPORTUNITY_AGENT,
        objective="events query", facts=facts,
    )


def _agent(seeded_session, knowledge_service) -> EventsAgent:
    return EventsAgent(session=seeded_session, knowledge_service=knowledge_service, llm_provider=MockLLMProvider())


def _upstream_academic_facts(seeded_session) -> dict:
    timetable = [t.model_dump(mode="json") for t in academic_service.get_timetable(seeded_session, DEMO_STUDENT)]
    exams = [e.model_dump(mode="json") for e in academic_service.get_exam_schedule(seeded_session, DEMO_STUDENT)]
    return {"timetable": timetable, "exams": exams}


def test_relevant_event_discovery_with_conflict_data_distinguishes_conflicted_events(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    facts = _upstream_academic_facts(seeded_session)
    facts["skill_gaps"] = ["Docker", "Kubernetes"]
    outcome = agent.handle(
        _message(DEMO_STUDENT, "Find workshops related to AI and my skill gaps that don't conflict with my classes or exams", extra_facts=facts)
    )

    assert outcome.verification.status == VerificationStatus.VERIFIED
    assessments = {a["event"]["title"]: a for a in outcome.agent_result.facts["assessments"]}

    ai_workshop = assessments["Artificial Intelligence & Deep Learning Workshop"]
    assert ai_workshop["timetable_conflicts"] == []
    assert ai_workshop["exam_conflicts"] == []
    assert ai_workshop["already_registered"] is True
    assert ai_workshop["registration_status"] == "confirmed"

    # Hackathon/Robotics aren't topically relevant to "AI"/Docker/Kubernetes,
    # so they correctly don't appear here -- proven separately below.
    assert "Hackathon Kickoff Session" not in assessments


def test_timetable_conflict_is_flagged_not_hidden(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    facts = _upstream_academic_facts(seeded_session)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find hackathon events that conflict with my classes or exams", extra_facts=facts))

    assessments = {a["event"]["title"]: a for a in outcome.agent_result.facts["assessments"]}
    hackathon = assessments["Hackathon Kickoff Session"]
    assert len(hackathon["timetable_conflicts"]) == 1
    assert hackathon["timetable_conflicts"][0]["course_code"] == "CS301"
    # Still reported as AVAILABLE (registerable) -- conflict is a separate, not-conflated dimension.
    assert hackathon["availability"] == "available"


def test_exam_conflict_is_flagged(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    facts = _upstream_academic_facts(seeded_session)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find robotics events that conflict with my classes or exams", extra_facts=facts))

    assessments = {a["event"]["title"]: a for a in outcome.agent_result.facts["assessments"]}
    robotics = assessments["Robotics Expo"]
    assert len(robotics["exam_conflicts"]) == 1
    assert robotics["exam_conflicts"][0]["course_code"] == "CS302"


def test_full_event_is_not_available(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find startup pitch night events"))
    assessments = {a["event"]["title"]: a for a in outcome.agent_result.facts["assessments"]}
    assert assessments["Startup Pitch Night"]["availability"] == "full"


def test_without_upstream_conflict_data_check_is_not_performed(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find AI workshop events"))
    assessments = outcome.agent_result.facts["assessments"]
    assert assessments
    assert all(a["conflict_check_performed"] is False for a in assessments)
    # Not having conflict data is a valid degraded mode (a standalone events
    # query, not depending on an Academic task), so verification still succeeds.
    assert outcome.verification.status == VerificationStatus.VERIFIED


def test_unknown_student_is_failed(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message("STU-NOPE-999", "Find AI workshop events"))
    assert outcome.verification.status == VerificationStatus.FAILED
    assert outcome.agent_result.status == AgentResultStatus.FAILED


def test_agent_never_writes_to_the_database(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message(DEMO_STUDENT, "Find AI workshop events"))
    assert not seeded_session.new
    assert not seeded_session.dirty
    assert not seeded_session.deleted
