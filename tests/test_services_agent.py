"""End-to-end tests for the ServicesAgent (real seeded DB + real RAG fixtures,
MockLLMProvider -- no live LLM, no network).
"""
from __future__ import annotations

from app.agents.services.agent import ServicesAgent
from app.llm.providers.mock import MockLLMProvider
from app.schemas.agent import AgentMessage
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus

DEMO_STUDENT = "STU-DEMO-001"
CASE_QUERY = "Check the status of my campus complaints, identify any overdue cases and explain the applicable grievance procedures."


def _message(student_id: str, query: str) -> AgentMessage:
    return AgentMessage(
        message_id="msg-1", mission_id="mission-1", task_id="task-1",
        source=AgentName.MISSION_ORCHESTRATOR, target=AgentName.CAMPUS_SERVICES_AGENT,
        objective="services query", facts={"student_id": student_id, "query": query},
    )


def _agent(seeded_session, knowledge_service) -> ServicesAgent:
    return ServicesAgent(session=seeded_session, knowledge_service=knowledge_service, llm_provider=MockLLMProvider())


def test_case_status_reports_known_sla_breaches(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, CASE_QUERY))

    assert outcome.verification.status == VerificationStatus.VERIFIED
    assert outcome.agent_result.status == AgentResultStatus.SUCCESS

    by_code = {a["case"]["case_code"]: a for a in outcome.agent_result.facts["case_assessments"]}
    assert set(by_code) == {"CASE-0001", "CASE-0002", "CASE-0003"}

    assert by_code["CASE-0001"]["response_breached"] is True
    assert by_code["CASE-0001"]["resolution_breached"] is False

    assert by_code["CASE-0002"]["response_breached"] is False
    assert by_code["CASE-0002"]["resolution_breached"] is True

    assert by_code["CASE-0003"]["response_breached"] is True
    assert by_code["CASE-0003"]["resolution_breached"] is True


def test_case_ownership_is_scoped_to_the_requesting_student(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, CASE_QUERY))
    for assessment in outcome.agent_result.facts["case_assessments"]:
        assert assessment["case"]["student_code"] == DEMO_STUDENT


def test_evidence_is_grounded_and_public_only(seeded_session, knowledge_service) -> None:
    """The admin_only internal escalation SOP must never surface in a student-facing response."""
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, CASE_QUERY))
    assert outcome.agent_result.evidence
    assert all(e.document_id != "internal-case-escalation-sop" for e in outcome.agent_result.evidence)


def test_unknown_student_is_failed(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message("STU-NOPE-999", CASE_QUERY))
    assert outcome.verification.status == VerificationStatus.FAILED
    assert outcome.agent_result.status == AgentResultStatus.FAILED


def test_student_with_no_cases_is_needs_review_not_fabricated(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    # STU2023006 has no seeded campus cases.
    outcome = agent.handle(_message("STU2023006", CASE_QUERY))
    assert outcome.agent_result.facts.get("case_assessments") in (None, [])
    assert outcome.verification.status == VerificationStatus.NEEDS_REVIEW


def test_agent_never_writes_to_the_database(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message(DEMO_STUDENT, CASE_QUERY))
    assert not seeded_session.new
    assert not seeded_session.dirty
    assert not seeded_session.deleted
