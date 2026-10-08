"""CAMPUS AI: the enriched demo campus, organization-scoped knowledge and grounded answers.

One throwaway SQLite database per module: the shared base seed (``run_seed``), the development accounts and the
CAMPUS AI enrichment (``scripts/seed_demo_campus.py``), pinned to a Wednesday 12:30 IST so "today" is the same on
every run. Knowledge is indexed with the offline deterministic embedding. No live model is ever called: synthesis
is exercised with in-process fakes.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.auth.accounts import seed_dev_accounts
from app.db.models import AuthAccount, Organization
from app.db.models.assignment import Assignment, AssignmentSubmission, AssignmentTarget
from app.db.models.faculty import AttendanceSession, AttendanceSessionStatus, TeachingAssignment
from app.db.models.identity import Student
from app.db.session import create_db_engine, create_session_factory, init_db
from app.db.tenant_session import TenantSessionFactory
from app.llm.base import LLMMalformedOutputError, LLMTransientError
from app.rag.campus_knowledge import (
    CAMPUS_KNOWLEDGE_DIR, MAX_CHUNKS, build_campus_knowledge, index_campus_knowledge, open_campus_store,
)
from app.rag.embeddings import get_embedding_provider
from app.schemas.enums import UserRole
from app.schemas.grounded import NOT_FOUND_ANSWER, ConversationTurn, GroundedSynthesis
from app.services.grounded_answers import (
    CONTEXT_TOKEN_BUDGET, GroundedAnswerService, build_prompt, classify, prompt_tokens,
)
from app.services.grounded_facts import FactResult, GroundedIdentity
from scripts.seed_data import run_seed
from scripts.seed_demo_campus import DemoSeedRefused, require_local_sqlite, run_demo_enrichment

IST = timezone(timedelta(hours=5, minutes=30))
PASSWORD = "campus-ai-test-pw-1"
ORG_SLUG = "campusnexus-demo"
PENDING_LAB4 = {"Aditi Rao", "Sanya Kapoor", "Farhan Ali", "Harsh Vardhan", "Omkar Patil"}


def _pinned_now() -> datetime:
    today = datetime.now(IST).date()
    wednesday = today + timedelta(days=(2 - today.weekday()) % 7)
    return datetime.combine(wednesday, time(12, 30), tzinfo=IST).astimezone(timezone.utc)


NOW = _pinned_now()


@pytest.fixture(scope="module")
def campus(tmp_path_factory):
    root: Path = tmp_path_factory.mktemp("campus_ai")
    engine = create_db_engine(db_path=root / "campus.db")
    init_db(engine)
    with create_session_factory(engine)() as s:
        run_seed(s)
        seed_dev_accounts(s, PASSWORD)
    first = run_demo_enrichment(engine, now=NOW)
    second = run_demo_enrichment(engine, now=NOW)
    store = open_campus_store(chroma_path=root / "chroma", embedding_provider=get_embedding_provider("deterministic"))
    with create_session_factory(engine)() as s:
        org = s.execute(select(Organization.id).where(Organization.slug == ORG_SLUG)).scalar_one()
        accounts = {a.email: a for a in s.execute(select(AuthAccount)).scalars()}
    index_campus_knowledge(CAMPUS_KNOWLEDGE_DIR, vector_store=store, organization_id=org, organization_slug=ORG_SLUG)
    yield {"engine": engine, "org": org, "accounts": accounts, "store": store, "first": first, "second": second,
           "knowledge": build_campus_knowledge(store), "factory": TenantSessionFactory(engine), "root": root}
    engine.dispose()


def student(campus, code: str = "STU-DEMO-001") -> GroundedIdentity:
    account = campus["accounts"]["student@campusnexus.local"]
    return GroundedIdentity(campus["org"], account.id, UserRole.STUDENT, "Aditi Rao", student_code=code)


def faculty(campus) -> GroundedIdentity:
    account = campus["accounts"]["faculty@campusnexus.local"]
    return GroundedIdentity(campus["org"], account.id, UserRole.FACULTY, "Dr. Ashok Verma",
                            faculty_profile_id=account.linked_faculty_id)


def ask(campus, identity, question, service=None, history=()):
    service = service or GroundedAnswerService(campus["knowledge"], clock=lambda: NOW)
    with campus["factory"].open_tenant_session(campus["org"]) as session:
        return service.answer(session, identity, question, history)


# --- Seed --------------------------------------------------------------------------------------------------------


def test_the_enrichment_is_idempotent_and_coherent(campus) -> None:
    first, second = campus["first"], campus["second"]
    assert first.assignments == 5 and first.exams == 3 and first.attendance_sessions > 0 and first.attendance_marks > 0
    assert first.guardian_missions == 5  # three live assignments + two scheduled exams, via the real services
    assert all(value == 0 for value in second.__dict__.values())  # a second run adds nothing


def test_the_demo_seed_refuses_a_non_sqlite_database() -> None:
    class FakeEngine:
        class dialect:  # noqa: N801
            name = "postgresql"

    with pytest.raises(DemoSeedRefused):
        require_local_sqlite(FakeEngine())


# --- Student -----------------------------------------------------------------------------------------------------


def test_timetable_returns_the_seeded_classes_for_today(campus) -> None:
    answer = ask(campus, student(campus), "Show my timetable")
    assert answer.found and answer.intent == "timetable"
    # Wednesday: CS303 10:00 (base timetable) and CS301 11:15 (CAMPUS AI slot).
    assert "10:00-11:00 Computer Networks (CS303)" in answer.answer
    assert "11:15-12:15 Operating Systems (CS301)" in answer.answer


def test_attendance_is_real_and_non_zero(campus) -> None:
    answer = ask(campus, student(campus), "What's my attendance?")
    assert "Operating Systems (CS301): 34/50 = 68.0%" in answer.answer
    assert "Database Management Systems (CS302): 47/50 = 94.0%" in answer.answer
    assert "required: 75" in answer.answer and answer.citations[0].source_id == "CAI-POL-ACAD-001"


def test_next_exam_is_the_seeded_quiz(campus) -> None:
    answer = ask(campus, student(campus), "When is my next exam?")
    assert answer.answer.startswith("Your next exam is Quiz 2: Transport Layer - Computer Networks (CS303)")


def test_pending_assignments_are_exactly_the_unsubmitted_ones(campus) -> None:
    answer = ask(campus, student(campus), "What are my pending assignments?")
    pending = [line[2:].split(" (")[0] for line in answer.answer.splitlines() if line.startswith("- ")]
    assert pending == ["Lab 4: Socket Programming Report", "SE Case Study: Requirements Specification"]
    assert "Submitted: 3" in answer.answer


def test_exam_eligibility_combines_sql_rag_and_the_deterministic_rule(campus) -> None:
    answer = ask(campus, student(campus), "Am I eligible for my OS exam?")
    assert answer.intent == "exam_eligibility"
    assert "not currently eligible" in answer.answer and "34/50 classes = 68.0%" in answer.answer
    assert "required 75" in answer.answer and "next 14 classes" in answer.answer
    assert {c.source_id for c in answer.citations} == {"CAI-POL-ACAD-001", "CAI-POL-ACAD-002"}


def test_placement_query_returns_seeded_opportunities(campus) -> None:
    answer = ask(campus, student(campus), "Show placement opportunities")
    assert "AI Software Engineering Intern at NimbusCloud Technologies" in answer.answer
    assert "already applied (under_review)" in answer.answer


def test_hostel_policy_retrieves_and_cites_the_right_document(campus) -> None:
    answer = ask(campus, student(campus), "What is the hostel complaint policy?")
    assert answer.intent == "policy" and answer.found
    assert {c.source_id for c in answer.citations} <= {"CAI-SOP-HOS-002", "CAI-POL-HOS-001"}
    assert "[CAI-SOP-HOS-002]" in answer.answer and "Campus Services case in the hostel category" in answer.answer


def test_no_record_means_the_not_found_answer(campus) -> None:
    other = student(campus, code="STU2023004")  # semester 3: no assignments were published to this class
    assert ask(campus, other, "What are my pending assignments?").answer == NOT_FOUND_ANSWER


def test_action_requests_are_not_answered_from_records(campus) -> None:
    assert classify("Register me for the hackathon", UserRole.STUDENT) is None


# --- Faculty -----------------------------------------------------------------------------------------------------


def _pending_names(campus, title: str) -> set:
    with campus["factory"].open_tenant_session(campus["org"]) as s:
        assignment = s.execute(select(Assignment).where(Assignment.title == title)).scalar_one()
        targets = set(s.execute(select(AssignmentTarget.student_id).where(AssignmentTarget.assignment_id == assignment.id)).scalars())
        done = set(s.execute(select(AssignmentSubmission.student_id).where(AssignmentSubmission.assignment_id == assignment.id)).scalars())
        return {s.get(Student, sid).user.full_name for sid in targets - done}


def test_latest_assignment_pending_students_are_exactly_correct(campus) -> None:
    answer = ask(campus, faculty(campus), "Who hasn't submitted my latest assignment?")
    assert answer.intent == "faculty_assignment_pending"
    assert answer.answer.startswith("Latest assignment: Lab 4: Socket Programming Report (CS303)")
    listed = {line[2:].split(" (")[0] for line in answer.answer.splitlines() if line.startswith("- ")}
    assert listed == PENDING_LAB4 == _pending_names(campus, "Lab 4: Socket Programming Report")
    assert "7 of 12 students have submitted" in answer.answer


def test_faculty_attendance_summary_uses_real_sessions(campus) -> None:
    with campus["factory"].open_tenant_session(campus["org"]) as s:
        teaching = s.execute(select(TeachingAssignment).where(
            TeachingAssignment.faculty_id == faculty(campus).faculty_profile_id)).scalar_one()
        held = len(s.execute(select(AttendanceSession.id).where(
            AttendanceSession.teaching_assignment_id == teaching.id,
            AttendanceSession.status == AttendanceSessionStatus.CLOSED)).all())
    answer = ask(campus, faculty(campus), "Show attendance summary for my classes")
    assert held > 0 and f"{held} sessions held recently" in answer.answer
    assert "Farhan Ali 66.0%" in answer.answer  # below 75% for the semester


def test_exams_taught_are_only_the_faculty_members_own(campus) -> None:
    answer = ask(campus, faculty(campus), "What exams am I teaching?")
    assert "Quiz 2: Transport Layer (CS303)" in answer.answer and "Quiz 1: Physical and Data Link Layers" in answer.answer
    assert "CS301" not in answer.answer  # Internal Assessment 1 belongs to another faculty member's class


def test_faculty_cannot_reach_another_teachers_class(campus) -> None:
    answer = ask(campus, faculty(campus), "Who hasn't submitted the CS301 assignment?")
    assert answer.answer == NOT_FOUND_ANSWER


# --- RAG ---------------------------------------------------------------------------------------------------------


def test_top_chunks_are_relevant_and_bounded(campus) -> None:
    chunks = campus["knowledge"].search("hostel complaint escalation chief warden", organization_id=campus["org"], top_k=20)
    assert 1 <= len(chunks) <= MAX_CHUNKS
    assert chunks[0].document_type == "hostel_complaint_procedure"


def test_organization_metadata_is_respected(campus, tmp_path) -> None:
    other_doc = (CAMPUS_KNOWLEDGE_DIR / "library_policy.md").read_text(encoding="utf-8").replace(
        "organization: campusnexus-demo", "organization: northfield-demo").replace(
        "5 rupees per day", "50 rupees per day")
    (tmp_path / "library_policy.md").write_text(other_doc, encoding="utf-8")
    summary = index_campus_knowledge(tmp_path, vector_store=campus["store"], organization_id=999,
                                     organization_slug="northfield-demo")
    assert summary.document_count == 1
    mine = campus["knowledge"].search("library overdue fine", organization_id=campus["org"], document_types=["library_policy"])
    theirs = campus["knowledge"].search("library overdue fine", organization_id=999, document_types=["library_policy"])
    assert mine and all("5 rupees" in c.text for c in mine if "fine" in c.section.lower())
    assert theirs and any("50 rupees" in c.text for c in theirs)
    assert campus["knowledge"].search("library overdue fine", organization_id=None) == []
    skipped = index_campus_knowledge(CAMPUS_KNOWLEDGE_DIR, vector_store=campus["store"], organization_id=999,
                                     organization_slug="northfield-demo")
    assert skipped.document_count == 0 and skipped.skipped_documents == 11  # another institution's documents


def test_unrelated_documents_are_not_injected(campus) -> None:
    answer = ask(campus, student(campus), "What is the library policy for overdue books?")
    assert answer.citations and {c.source_id for c in answer.citations} == {"CAI-POL-LIB-001"}
    assert ask(campus, student(campus), "When is my next exam?").citations == []  # an SQL-only question


# --- Context budget and synthesis ----------------------------------------------------------------------------------


def test_prompt_contains_only_bounded_context() -> None:
    identity = GroundedIdentity(1, 1, UserRole.STUDENT, "A", student_code="S")
    result = FactResult(facts=[(f"fact {i}", "x" * 2000) for i in range(30)], draft="draft", found=True)
    history = [ConversationTurn(user="u" * 300, assistant="a" * 300) for _ in range(3)]
    prompt = build_prompt(classify("Show my timetable", UserRole.STUDENT), identity, "q" * 2000, result, history)
    assert len(prompt.facts) <= 12 and all(len(str(f.value)) <= 600 for f in prompt.facts)
    assert len(prompt.question) == 500 and len(prompt.recent_conversation) <= 3
    assert prompt_tokens(prompt) <= CONTEXT_TOKEN_BUDGET


class FakeProvider:
    name, is_live, model_name = "openrouter", True, "google/gemma-4-26b-a4b-it:free"
    last_model_used = "nvidia/nemotron-3.5-lightning:free"

    def __init__(self, reply) -> None:
        self.reply, self.prompts = reply, []

    def synthesize_grounded_answer(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(prompt) if callable(self.reply) else self.reply


def _service(campus, reply, **kwargs) -> tuple:
    provider = FakeProvider(reply)
    return GroundedAnswerService(campus["knowledge"], provider, clock=lambda: NOW, **kwargs), provider


def test_a_faithful_synthesis_is_accepted_and_attributed_to_the_answering_model(campus) -> None:
    names = ", ".join(sorted(PENDING_LAB4))
    service, provider = _service(campus, GroundedSynthesis(
        answer=f"For Lab 4: Socket Programming Report, these students have not submitted yet: {names}.",
        used_fact_ids=["F1", "F3"]))
    answer = ask(campus, faculty(campus), "Who hasn't submitted my latest assignment?", service)
    assert answer.synthesis_status == "accepted" and answer.answered_by == "nvidia/nemotron-3.5-lightning:free"
    sent = provider.prompts[0].payload()
    assert set(sent) <= {"intent", "caller_role", "caller_name", "question", "facts", "sources",
                         "recent_conversation", "draft_answer"}
    assert prompt_tokens(provider.prompts[0]) < 1500


@pytest.mark.parametrize("synthesis,code", [
    (GroundedSynthesis(answer="Aditi Rao, Sanya Kapoor, Farhan Ali, Harsh Vardhan and Omkar Patil owe 42 reports."),
     "UNGROUNDED_NUMBER"),
    (GroundedSynthesis(answer="Aditi Rao and Sanya Kapoor have not submitted."), "MISSING_REQUIRED_NAME"),
    (GroundedSynthesis(answer="Everyone is late.", used_fact_ids=["F9"]), "UNKNOWN_FACT_ID"),
])
def test_an_unfaithful_synthesis_is_rejected_and_the_records_answer_kept(campus, synthesis, code) -> None:
    service, _ = _service(campus, synthesis)
    answer = ask(campus, faculty(campus), "Who hasn't submitted my latest assignment?", service)
    assert answer.synthesis_status == f"rejected:{code}" and answer.answered_by == "deterministic"
    assert all(name in answer.answer for name in PENDING_LAB4)


def test_malformed_or_unavailable_providers_fail_safely(campus) -> None:
    for error, status in ((LLMMalformedOutputError("bad json"), "rejected:MALFORMED_OUTPUT"),
                          (LLMTransientError("down", provider="openrouter", model="m", kind="network"), "unavailable:network")):
        service, _ = _service(campus, error)
        answer = ask(campus, student(campus), "When is my next exam?", service)
        assert answer.synthesis_status == status and answer.answered_by == "deterministic"
        assert "Quiz 2: Transport Layer" in answer.answer


def test_auto_mode_moves_to_the_local_model_only_on_a_transport_failure(campus) -> None:
    class Local:
        provider_name, model_name = "ollama", "qwen3:4b"

        def synthesize(self, prompt):
            return GroundedSynthesis(answer=prompt.draft_answer[:1200])

    down = LLMTransientError("down", provider="openrouter", model="m", kind="network")
    service, _ = _service(campus, down, local=Local(), mode="auto")
    assert ask(campus, student(campus), "When is my next exam?", service).answered_by == "qwen3:4b"
    service, _ = _service(campus, down, local=Local(), mode="cloud")  # cloud mode never calls the local model
    assert ask(campus, student(campus), "When is my next exam?", service).answered_by == "deterministic"
    malformed = LLMMalformedOutputError("bad")
    service, _ = _service(campus, malformed, local=Local(), mode="auto")  # not connectivity: no switch
    assert ask(campus, student(campus), "When is my next exam?", service).answered_by == "deterministic"


# --- Nexus end to end (offline mock brain) -------------------------------------------------------------------------


@pytest.fixture()
def nexus_client(campus, knowledge_service):
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.llm.providers.mock import MockLLMProvider

    app = create_app(session_factory=create_session_factory(campus["engine"]), knowledge_service=knowledge_service,
                     llm_provider=MockLLMProvider(), clock=lambda: NOW, campus_knowledge=campus["knowledge"])
    with TestClient(app) as client:
        yield client


def _nexus(client, email: str, message: str) -> dict:
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    reply = client.post("/agentos/assistant/message", json={"message": message},
                        headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_nexus_answers_faculty_and_students_from_campus_records(nexus_client) -> None:
    body = _nexus(nexus_client, "faculty@campusnexus.local", "Who hasn't submitted my latest assignment?")
    assert body["status"] == "completed" and all(name in body["assistant_message"] for name in PENDING_LAB4)
    body = _nexus(nexus_client, "student@campusnexus.local", "When is my next exam?")
    assert "Quiz 2: Transport Layer" in body["assistant_message"]
    body = _nexus(nexus_client, "student@campusnexus.local", "What is the hostel complaint policy?")
    assert "[CAI-SOP-HOS-002]" in body["assistant_message"]
