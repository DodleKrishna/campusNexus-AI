"""End-to-end tests for the CareerAgent (real seeded DB + real RAG fixtures,
MockLLMProvider -- no live LLM, no network, per CLAUDE.md Testing Rules).
"""
from __future__ import annotations

from app.agents.career.agent import CareerAgent
from app.llm.providers.mock import MockLLMProvider
from app.schemas.agent import AgentMessage
from app.schemas.career import OpportunityEligibilityStatus
from app.schemas.enums import AgentName, AgentResultStatus, VerificationStatus

DEMO_STUDENT = "STU-DEMO-001"


def _message(student_id: str, query: str) -> AgentMessage:
    return AgentMessage(
        message_id="msg-1", mission_id="mission-1", task_id="task-1",
        source=AgentName.MISSION_ORCHESTRATOR, target=AgentName.CAREER_AGENT,
        objective="career query", facts={"student_id": student_id, "query": query},
    )


def _agent(seeded_session, knowledge_service) -> CareerAgent:
    return CareerAgent(session=seeded_session, knowledge_service=knowledge_service, llm_provider=MockLLMProvider())


def test_opportunity_discovery_matches_known_eligible_and_ineligible_scenarios(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find internships I'm eligible for and identify my skill gaps"))

    assert outcome.verification.status == VerificationStatus.VERIFIED
    assert outcome.agent_result.status == AgentResultStatus.SUCCESS

    eligibilities = outcome.agent_result.facts["eligibilities"]
    by_title = {e["opportunity"]["title"]: e for e in eligibilities}

    assert by_title["AI Software Engineering Intern"]["status"] == "eligible"
    assert by_title["Quant Research Intern"]["status"] == "not_eligible"
    assert any("CGPA" in r for r in by_title["Quant Research Intern"]["blocking_reasons"])
    assert by_title["Embedded Systems Intern"]["status"] == "not_eligible"
    assert any("department" in r for r in by_title["Embedded Systems Intern"]["blocking_reasons"])
    assert by_title["Cloud DevOps Intern"]["status"] == "not_eligible"
    assert set(by_title["Cloud DevOps Intern"]["missing_mandatory_skills"]) == {"Docker", "Kubernetes", "Cloud Computing (AWS)"}
    assert "Data Analyst Intern" not in by_title  # expired -- excluded from OPEN opportunities entirely


def test_already_applied_is_reflected_in_eligibility(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find internships I'm eligible for and identify my skill gaps"))
    eligibilities = {e["opportunity"]["title"]: e for e in outcome.agent_result.facts["eligibilities"]}
    assert eligibilities["AI Software Engineering Intern"]["already_applied"] is True
    assert eligibilities["AI Software Engineering Intern"]["application_status"] == "under_review"


def test_skill_gaps_are_actionable_not_blocked_by_other_criteria(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find internships I'm eligible for and identify my skill gaps"))
    skill_gaps = set(outcome.agent_result.facts["skill_gaps"])
    # From Cloud DevOps Intern / Full-Stack Developer Intern -- both otherwise
    # within the student's department/year/CGPA reach.
    assert {"Docker", "Kubernetes", "Cloud Computing (AWS)", "React", "Node.js"}.issubset(skill_gaps)
    # Skills only relevant to department-blocked opportunities must not appear.
    assert "Circuit Design" not in skill_gaps
    assert "AutoCAD" not in skill_gaps


def test_evidence_is_grounded_and_public_only(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "Find internships I'm eligible for and identify my skill gaps"))
    assert outcome.agent_result.evidence
    assert all(e.document_id != "internal-case-escalation-sop" for e in outcome.agent_result.evidence)


def test_application_status_intent(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message(DEMO_STUDENT, "What is the status of my applications?"))
    assert outcome.agent_result.facts["intent"] == "application_status"
    applications = outcome.agent_result.facts["applications"]
    assert any(a["opportunity_title"] == "AI Software Engineering Intern" for a in applications)


def test_unknown_student_is_failed(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    outcome = agent.handle(_message("STU-NOPE-999", "Find internships I'm eligible for"))
    assert outcome.verification.status == VerificationStatus.FAILED
    assert outcome.agent_result.status == AgentResultStatus.FAILED


def test_agent_never_writes_to_the_database(seeded_session, knowledge_service) -> None:
    agent = _agent(seeded_session, knowledge_service)
    agent.handle(_message(DEMO_STUDENT, "Find internships I'm eligible for and identify my skill gaps"))
    assert not seeded_session.new
    assert not seeded_session.dirty
    assert not seeded_session.deleted
