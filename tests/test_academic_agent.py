"""End-to-end tests for the AcademicAgent (real seeded DB + real RAG fixtures,
MockLLMProvider -- no live LLM, no network, per CLAUDE.md Testing Rules).
"""
from __future__ import annotations

from decimal import Decimal

from app.agents.academic.agent import AcademicAgent
from app.llm.providers.mock import MockLLMProvider
from app.schemas.agent import AgentMessage, AgentResult
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus
from app.schemas.verification import VerificationResult

DEMO_STUDENT = "STU-DEMO-001"


def _message(student_id: str, query: str, *, as_of: str | None = None) -> AgentMessage:
    facts = {"student_id": student_id, "query": query}
    if as_of:
        facts["as_of"] = as_of
    return AgentMessage(
        message_id="msg-1",
        mission_id="mission-1",
        task_id="task-1",
        source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACADEMIC_AGENT,
        objective="answer an academic question",
        facts=facts,
    )


def _agent(seeded_session, knowledge_service) -> AcademicAgent:
    return AcademicAgent(session=seeded_session, knowledge_service=knowledge_service, llm_provider=MockLLMProvider())


def test_outcome_is_built_from_structured_pydantic_models(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "What's my attendance in OS?"))
    assert isinstance(outcome.agent_result, AgentResult)
    assert isinstance(outcome.verification, VerificationResult)
    assert isinstance(outcome.response_text, str)


def test_attendance_status_end_to_end_matches_deterministic_rule(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "What's my attendance in Operating Systems?"))

    assert outcome.verification.status == VerificationStatus.VERIFIED
    assert outcome.agent_result.status == AgentResultStatus.SUCCESS
    attendance = outcome.agent_result.facts["attendance"]
    assert attendance["current_percentage"] == "68.0"
    assert attendance["eligible_now"] is False
    assert attendance["classes_needed_to_reach_threshold"] == 14
    assert "68.0%" in outcome.response_text


def test_evidence_is_propagated_and_traceable_to_the_active_policy(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "What's my attendance in OS?"))
    assert outcome.agent_result.evidence
    assert any(e.document_id == "attendance-policy-v2" for e in outcome.agent_result.evidence)
    assert outcome.agent_result.facts["threshold"]["document_id"] == "attendance-policy-v2"
    assert outcome.agent_result.facts["threshold"]["required_percentage"] == "75"


def test_historical_as_of_resolves_to_the_older_policy_version(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(
        _message(DEMO_STUDENT, "What's my attendance in OS?", as_of="2024-06-01")
    )
    assert outcome.agent_result.facts["threshold"]["document_id"] == "attendance-policy-v1"
    assert outcome.agent_result.facts["threshold"]["required_percentage"] == "70"


def test_unknown_course_is_failed_and_never_fabricates_a_course(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "What's my attendance in Blockchain?"))
    assert outcome.verification.status == VerificationStatus.FAILED
    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert "attendance" not in outcome.agent_result.facts
    assert outcome.agent_result.facts["course_resolution"]["course_code"] is None


def test_ambiguous_course_is_needs_review_and_lists_real_candidates_only(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "What's my attendance in Systems?"))
    assert outcome.verification.status == VerificationStatus.NEEDS_REVIEW
    candidates = outcome.agent_result.facts["course_resolution"]["candidates"]
    assert set(candidates) == {"CS301", "CS302"}
    assert "CS301" in outcome.response_text and "CS302" in outcome.response_text


def test_unknown_student_is_failed(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message("STU-NOPE-999", "What's my attendance in OS?"))
    assert outcome.verification.status == VerificationStatus.FAILED
    assert outcome.agent_result.status == AgentResultStatus.FAILED
    assert any("Student record not found" in issue for issue in outcome.verification.issues)


def test_unsupported_request_is_failed_not_a_guess(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "Can you order me a pizza?"))
    assert outcome.agent_result.facts["intent"] == "unknown"
    assert outcome.verification.status == VerificationStatus.FAILED


def test_agent_never_writes_to_the_database(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message(DEMO_STUDENT, "How many classes do I need to attend in OS to reach the required attendance?"))
    assert not seeded_session.new
    assert not seeded_session.dirty
    assert not seeded_session.deleted


def test_timetable_and_exam_schedule_intents_return_all_enrolled_courses(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)

    timetable_outcome = agent.handle(_message(DEMO_STUDENT, "What is my timetable?"))
    assert timetable_outcome.verification.status == VerificationStatus.VERIFIED
    timetable_codes = {e["course_code"] for e in timetable_outcome.agent_result.facts["timetable"]}
    assert timetable_codes == {"CS301", "CS302", "CS303", "CS304"}

    exam_outcome = agent.handle(_message(DEMO_STUDENT, "When are my exams?"))
    assert exam_outcome.verification.status == VerificationStatus.VERIFIED
    exam_codes = {e["course_code"] for e in exam_outcome.agent_result.facts["exams"]}
    assert exam_codes == {"CS301", "CS302", "CS303", "CS304"}
