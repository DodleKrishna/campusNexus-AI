"""Phase 13 -- candidate selection and continuation of the same mission.

"Find a suitable event, check my schedule and prepare my registration" names
no event. The mission discovers candidates, checks them deterministically
against the verified timetable and exams, and waits. The student selects one
(server-validated, persisted as USER_SELECTION); the same mission then
proposes, pre-checks and requests approval, and only an approver's decision
plus a clean execute-time recheck lets the write happen.

All offline: the mock planner plans the discovery tasks and declares the
action in ``selection_required_actions``; selection, refresh and continuation
make no LLM call at all.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

import pytest
from sqlalchemy import func, select

from app.db.base import utc_now
from app.db.models.academic import Enrollment, Exam
from app.db.models.events import Event, EventRegistration
from app.db.models.identity import Student
from app.db.models.mission import ActionCandidateRecord, AgentRun, ApprovalRecord, ToolCallRecord
from app.db.repositories.missions import get_latest_approval_for_step, list_approvals_for_step
from app.graph.orchestrator import MissionOrchestrator, SelectionError, user_selection_required
from app.llm.providers.mock import MockLLMProvider
from app.rules.action_preconditions import check_event_registration, resolve_target_provenance
from app.rules.candidate_status import derive_candidate_status
from app.schemas.action import TargetSource
from app.schemas.enums import AgentName, ApprovalStatus, MissionStatus, ToolExecutionStatus, UserRole
from app.schemas.events import EventSummary, TimetableConflict
from app.schemas.selection import CandidateStatus, SelectedTarget, SelectionResult
from app.services import target_selection
from app.services.approval_gate import ApprovalGate, ApprovalGateError
from app.services.context import ContextService
from tests.test_phase12b_live_correctness import _orchestrator

STUDENT = "STU-DEMO-001"
UNNAMED_GOAL = "Find a suitable event, check my schedule and prepare my registration."
NAMED_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
CODING = 10  # Competitive Coding Contest: conflict-free, open
PHOTO = 14  # Photography Contest Exhibition: conflict-free, open
AI_WORKSHOP = 1  # already registered by the demo student
HACKATHON = 2  # clashes with CS301 (Monday class)
ROBOTICS = 3  # clashes with the CS302 exam
PITCH = 4  # at capacity


class CountingLLM(MockLLMProvider):
    """The mock provider, recording every public call made to it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: List[str] = []

    def __getattribute__(self, name: str):
        attr = super().__getattribute__(name)
        if callable(attr) and not name.startswith("_") and name != "calls":
            calls = super().__getattribute__("calls")

            def counted(*args, **kwargs):
                calls.append(name)
                return attr(*args, **kwargs)

            return counted
        return attr


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _discover(orchestrator: MissionOrchestrator) -> str:
    final = orchestrator.run_mission(UNNAMED_GOAL, user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    assert final["mission_status"] == MissionStatus.COMPLETED
    return final["mission_id"]


def _select(orchestrator: MissionOrchestrator, mission_id: str, event_id: int):
    return orchestrator.select_target(
        mission_id, tool_name="register_event", resource_type="event", resource_id=event_id, selected_by=STUDENT
    )


def _candidates(session_factory, mission_id: str) -> Dict[int, Any]:
    with session_factory() as session:
        return {c.resource_id: c for c in target_selection.list_candidates(session, mission_id)}


def _approvals(session_factory, mission_id: str) -> List[ApprovalRecord]:
    with session_factory() as session:
        return list(session.execute(select(ApprovalRecord).where(ApprovalRecord.mission_id == mission_id)).scalars())


def _tool_calls(session_factory, mission_id: str) -> List[ToolCallRecord]:
    with session_factory() as session:
        return list(session.execute(select(ToolCallRecord).where(ToolCallRecord.mission_id == mission_id)).scalars())


def _registrations(session_factory, event_id: int) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == STUDENT)).scalar_one()
        return session.execute(
            select(func.count()).select_from(EventRegistration).where(
                EventRegistration.event_id == event_id, EventRegistration.student_id == student.id
            )
        ).scalar_one()


def _mission(session_factory, mission_id: str):
    with session_factory() as session:
        context = ContextService(session)
        mission = context.get_mission(mission_id)
        plan = context.get_latest_plan_snapshot(mission_id)
        selections = context.list_active_target_selections(mission_id)
        return mission.status, plan, selections, user_selection_required(plan, mission.original_goal, selections)


def _audit(session_factory, mission_id: str) -> List[str]:
    with session_factory() as session:
        return [e.event_type for e in ContextService(session).list_audit_events(mission_id)]


def _approve_latest(session_factory, step_id: str) -> str:
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, step_id)
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
        return approval.approval_id


def _add_exam_over(session_factory, event_id: int) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == STUDENT)).scalar_one()
        enrollment = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().first()
        event = session.get(Event, event_id)
        exam = Exam(course_id=enrollment.course_id, exam_type="quiz", scheduled_start=event.start_at,
                    scheduled_end=event.end_at, location="Rescheduled Exam Hall")
        session.add(exam)
        session.commit()
        return exam.id


def _remove_exam(session_factory, exam_id: int) -> None:
    with session_factory() as session:
        session.delete(session.get(Exam, exam_id))
        session.commit()


def _set_event(session_factory, event_id: int, **fields: Any) -> None:
    with session_factory() as session:
        event = session.get(Event, event_id)
        for key, value in fields.items():
            setattr(event, key, value)
        session.commit()


@pytest.fixture()
def llm() -> CountingLLM:
    return CountingLLM()


@pytest.fixture()
def orchestrator(session_factory, seeded_session, knowledge_service, llm) -> MissionOrchestrator:
    return _orchestrator(session_factory, knowledge_service, llm)


# ---------------------------------------------------------------------------
# Deterministic candidate status (rule-level, boundaries included)
# ---------------------------------------------------------------------------


def _event(**overrides: Any) -> EventSummary:
    now = utc_now()
    base = dict(
        event_id=99, title="Test Event", description="d", category="workshop", organizer="o", location="Hall",
        start_at=now + timedelta(days=5), end_at=now + timedelta(days=5, hours=2),
        registration_deadline=now + timedelta(days=2), capacity=10, status="open",
    )
    base.update(overrides)
    return EventSummary(**base)


def _status(event: EventSummary, *, now=None, confirmed: int = 0, registered: bool = False, conflicts=None, checked: bool = True):
    checks = check_event_registration(
        student_exists=True, event=event, confirmed_registrations=confirmed, already_registered=registered,
        now=now or utc_now(), timetable_conflicts=conflicts or [], exam_conflicts=[], conflict_check_performed=checked,
    )
    return derive_candidate_status(checks)[0]


def test_candidate_status_rules_and_boundaries() -> None:
    event = _event()
    assert _status(event) == CandidateStatus.ELIGIBLE
    # Exact deadline is still open; one second later it has passed.
    assert _status(event, now=event.registration_deadline) == CandidateStatus.ELIGIBLE
    assert _status(event, now=event.registration_deadline + timedelta(seconds=1)) == CandidateStatus.DEADLINE_PASSED
    # Exact capacity is full; one seat short is not.
    assert _status(event, confirmed=9) == CandidateStatus.ELIGIBLE
    assert _status(event, confirmed=10) == CandidateStatus.FULL
    assert _status(event, registered=True) == CandidateStatus.ALREADY_REGISTERED
    assert _status(_event(status="scheduled")) == CandidateStatus.UNAVAILABLE
    clash = TimetableConflict(course_code="CS301", weekday=0, start_time="10:00", end_time="11:00")
    assert _status(event, conflicts=[clash]) == CandidateStatus.CONFLICT
    # A schedule that was never checked is never ELIGIBLE.
    assert _status(event, checked=False) == CandidateStatus.NEEDS_REVIEW
    checks = check_event_registration(
        student_exists=True, event=None, confirmed_registrations=0, already_registered=False, now=utc_now()
    )
    assert derive_candidate_status(checks)[0] == CandidateStatus.UNAVAILABLE


def test_user_selection_provenance_confirms_only_the_selected_resource() -> None:
    selection = SelectedTarget(
        selection_id="sel-1", mission_id="m", step_id="m-task-4", tool_name="register_event", resource_type="event",
        resource_id=CODING, title="Competitive Coding Contest", selected_by=STUDENT, selected_at=utc_now(),
    )
    ok = resolve_target_provenance("register_event", {"event_id": CODING}, UNNAMED_GOAL, selection)
    assert ok.source == TargetSource.USER_SELECTION and ok.target_value == "Competitive Coding Contest"
    other = resolve_target_provenance("register_event", {"event_id": PHOTO}, UNNAMED_GOAL, selection)
    assert other.source == TargetSource.UNCONFIRMED
    # Without a persisted selection the same constraints are never confirmed.
    assert resolve_target_provenance("register_event", {"event_id": CODING}, UNNAMED_GOAL).source == TargetSource.UNCONFIRMED


# ---------------------------------------------------------------------------
# A/B: discovery produces deterministically assessed candidates
# ---------------------------------------------------------------------------


def test_unnamed_registration_produces_candidates_and_requires_selection(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    status, plan, selections, required = _mission(session_factory, mission_id)

    assert required is True and selections == {}
    assert not any(t.agent == AgentName.ACTION_AGENT for t in plan.tasks)
    assert _approvals(session_factory, mission_id) == [] and _tool_calls(session_factory, mission_id) == []
    assert "action_candidates_recorded" in _audit(session_factory, mission_id)
    candidates = _candidates(session_factory, mission_id)
    assert len(candidates) > 5
    assert all(c.resource_id > 0 and c.title for c in candidates.values())


def test_candidate_statuses_come_from_the_deterministic_schedule_check(orchestrator, session_factory) -> None:
    candidates = _candidates(session_factory, _discover(orchestrator))

    assert candidates[CODING].assessment.status == CandidateStatus.ELIGIBLE and candidates[CODING].selectable
    assert candidates[HACKATHON].assessment.status == CandidateStatus.CONFLICT
    assert [c.course_code for c in candidates[HACKATHON].assessment.timetable_conflicts] == ["CS301"]
    assert candidates[ROBOTICS].assessment.status == CandidateStatus.CONFLICT
    assert [c.course_code for c in candidates[ROBOTICS].assessment.exam_conflicts] == ["CS302"]
    assert candidates[PITCH].assessment.status == CandidateStatus.FULL
    assert candidates[AI_WORKSHOP].assessment.status == CandidateStatus.ALREADY_REGISTERED
    # Built from the mission's verified Academic results, not the Events Agent's prose.
    assert candidates[CODING].assessment.schedule_source == target_selection.SCHEDULE_FROM_MISSION
    assert not any(c.selectable for c in candidates.values() if c.assessment.status != CandidateStatus.ELIGIBLE)


# ---------------------------------------------------------------------------
# C, I, J, K, L, M: select -> proposal -> one approval -> approve -> one write
# ---------------------------------------------------------------------------


def test_selecting_a_safe_event_continues_the_same_mission_to_one_approval(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    before = _registrations(session_factory, CODING)

    outcome = _select(orchestrator, mission_id, CODING)

    assert outcome.result == SelectionResult.SELECTED
    status, plan, selections, required = _mission(session_factory, mission_id)
    assert status == MissionStatus.NEEDS_APPROVAL and required is False
    selection = next(iter(selections.values()))
    assert selection.resource_id == CODING and selection.title == "Competitive Coding Contest"
    action = next(t for t in plan.tasks if t.agent == AgentName.ACTION_AGENT)
    assert action.task_id == selection.step_id and action.mission_id == mission_id
    assert action.constraints["event_id"] == CODING
    upstream = sorted(t.agent.value for t in plan.tasks if t.task_id in action.dependencies)
    assert upstream == ["academic_agent", "academic_agent", "events_opportunity_agent"]

    # J/I: a real proposal whose target provenance is the student's selection.
    with session_factory() as session:
        run = session.execute(select(AgentRun).where(AgentRun.step_id == action.task_id)).scalars().all()[-1]
    assert run.facts["target_provenance"]["source"] == TargetSource.USER_SELECTION.value
    assert run.facts["proposal"]["parameters"] == {"student_id": STUDENT, "event_id": CODING}
    assert run.facts["precheck_status"] == "verified"
    assert run.facts["proposal"]["supporting_facts"]["schedule_check"]["source"] == "upstream_academic_tasks"

    # K/L: exactly one approval, bound to the selected target; nothing written.
    approvals = _approvals(session_factory, mission_id)
    assert [a.status for a in approvals] == [ApprovalStatus.PENDING]
    assert approvals[0].approved_payload["arguments"]["event_id"] == CODING
    assert [t.status for t in _tool_calls(session_factory, mission_id)] == [ToolExecutionStatus.PENDING]
    assert _registrations(session_factory, CODING) == before

    # M: approve + resume writes exactly one registration, verified.
    _approve_latest(session_factory, action.task_id)
    final = orchestrator.resume_mission(mission_id)
    assert final["mission_status"] == MissionStatus.COMPLETED
    assert final["agent_results"][action.task_id].facts["postcondition_verified"] is True
    assert _registrations(session_factory, CODING) == before + 1
    assert _mission(session_factory, mission_id)[3] is False
    events = _audit(session_factory, mission_id)
    assert events.index("target_selected") < events.index("approval_requested") < events.index("approval_approved")

    # A completed action's selection can no longer change.
    with pytest.raises(SelectionError) as info:
        _select(orchestrator, mission_id, PHOTO)
    assert info.value.status_code == 409


# ---------------------------------------------------------------------------
# D, E, F, O: unsafe candidates are refused -- no proposal, no approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_id,expected", [(HACKATHON, CandidateStatus.CONFLICT), (PITCH, CandidateStatus.FULL)])
def test_unsafe_candidates_cannot_be_selected(orchestrator, session_factory, event_id, expected) -> None:
    mission_id = _discover(orchestrator)

    outcome = _select(orchestrator, mission_id, event_id)

    assert outcome.result == SelectionResult.BLOCKED and outcome.candidate.assessment.status == expected
    assert _approvals(session_factory, mission_id) == [] and _tool_calls(session_factory, mission_id) == []
    assert _mission(session_factory, mission_id)[2] == {}  # no selection recorded
    assert "target_selection_blocked" in _audit(session_factory, mission_id)


def test_expired_event_cannot_be_selected(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    _set_event(session_factory, CODING, registration_deadline=utc_now() - timedelta(hours=1))

    outcome = _select(orchestrator, mission_id, CODING)

    assert outcome.result == SelectionResult.BLOCKED
    assert outcome.candidate.assessment.status == CandidateStatus.DEADLINE_PASSED
    assert _approvals(session_factory, mission_id) == []


def test_event_that_became_unsafe_after_recommendation_is_blocked(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    assert _candidates(session_factory, mission_id)[CODING].selectable
    _add_exam_over(session_factory, CODING)

    outcome = _select(orchestrator, mission_id, CODING)

    assert outcome.result == SelectionResult.BLOCKED
    assert "Please choose another event" in outcome.message
    assert _approvals(session_factory, mission_id) == []
    # The stored candidate now says why.
    stored = _candidates(session_factory, mission_id)[CODING]
    assert stored.assessment.status == CandidateStatus.CONFLICT and not stored.selectable
    assert stored.assessment.schedule_source == target_selection.SCHEDULE_CURRENT


def test_event_becoming_full_after_recommendation_is_blocked(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    with session_factory() as session:
        taken = session.execute(select(func.count()).select_from(EventRegistration).where(EventRegistration.event_id == CODING)).scalar_one()
    _set_event(session_factory, CODING, capacity=taken)

    outcome = _select(orchestrator, mission_id, CODING)

    assert outcome.result == SelectionResult.BLOCKED
    assert outcome.candidate.assessment.status == CandidateStatus.FULL
    assert "full" in outcome.message


# ---------------------------------------------------------------------------
# G, H: nothing outside the mission's own persisted candidates is selectable
# ---------------------------------------------------------------------------


def test_fabricated_resource_id_is_rejected(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    with pytest.raises(SelectionError) as info:
        _select(orchestrator, mission_id, 99999)
    assert info.value.status_code == 404
    assert _approvals(session_factory, mission_id) == []


def test_candidate_of_another_mission_is_rejected(orchestrator, session_factory) -> None:
    discovery = _discover(orchestrator)
    other = orchestrator.run_mission(
        "Are there any workshops this month that don't clash with my classes or exams?",
        user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT,
    )["mission_id"]
    assert CODING in _candidates(session_factory, discovery)
    with pytest.raises(SelectionError) as info:
        _select(orchestrator, other, CODING)
    assert info.value.status_code == 422
    # A candidate row is scoped to its own mission.
    with session_factory() as session:
        assert ContextService(session).get_action_candidate(other, "register_event", "event", CODING) is None


def test_selection_on_a_mission_that_needs_none_is_rejected(orchestrator, session_factory) -> None:
    named = orchestrator.run_mission(NAMED_GOAL, user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    with pytest.raises(SelectionError):
        _select(orchestrator, named["mission_id"], CODING)


# ---------------------------------------------------------------------------
# N, Q: idempotent re-selection; a changed selection never reuses an approval
# ---------------------------------------------------------------------------


def test_repeated_selection_does_not_duplicate_anything(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    _select(orchestrator, mission_id, CODING)

    again = _select(orchestrator, mission_id, CODING)

    assert again.result == SelectionResult.ALREADY_SELECTED
    assert len(_approvals(session_factory, mission_id)) == 1
    assert len(_tool_calls(session_factory, mission_id)) == 1
    _, plan, selections, _ = _mission(session_factory, mission_id)
    assert len([t for t in plan.tasks if t.agent == AgentName.ACTION_AGENT]) == 1
    assert len(selections) == 1


def test_changing_selection_supersedes_the_old_approval(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    _select(orchestrator, mission_id, CODING)
    first = _approvals(session_factory, mission_id)[0]

    changed = _select(orchestrator, mission_id, PHOTO)

    assert changed.result == SelectionResult.SELECTED and changed.superseded_approval_id == first.approval_id
    approvals = {a.approval_id: a for a in _approvals(session_factory, mission_id)}
    assert approvals[first.approval_id].status == ApprovalStatus.EDIT_REQUIRED
    pending = [a for a in approvals.values() if a.status == ApprovalStatus.PENDING]
    assert len(pending) == 1 and pending[0].approved_payload["arguments"]["event_id"] == PHOTO
    old_call = next(t for t in _tool_calls(session_factory, mission_id) if t.tool_call_id == first.tool_call_id)
    assert old_call.status == ToolExecutionStatus.FAILED  # can never run
    # The approval for event A can never authorize anything now.
    with session_factory() as session, pytest.raises(ApprovalGateError):
        ApprovalGate(session).decide(first.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")
    _, plan, selections, _ = _mission(session_factory, mission_id)
    assert [s.resource_id for s in selections.values()] == [PHOTO]
    assert len([t for t in plan.tasks if t.agent == AgentName.ACTION_AGENT]) == 1  # replaced in place

    _approve_latest(session_factory, pending[0].step_id)
    final = orchestrator.resume_mission(mission_id)
    assert final["mission_status"] == MissionStatus.COMPLETED
    assert _registrations(session_factory, PHOTO) == 1 and _registrations(session_factory, CODING) == 0


# ---------------------------------------------------------------------------
# P: after approval, a new conflict still makes the approval STALE (Phase 11)
# ---------------------------------------------------------------------------


def test_conflict_after_approval_makes_the_approval_stale(orchestrator, session_factory) -> None:
    mission_id = _discover(orchestrator)
    _select(orchestrator, mission_id, CODING)
    step_id = next(iter(_mission(session_factory, mission_id)[2].values())).step_id
    approved_id = _approve_latest(session_factory, step_id)
    exam_id = _add_exam_over(session_factory, CODING)

    orchestrator.resume_mission(mission_id)

    with session_factory() as session:
        approval = session.get(ApprovalRecord, approved_id)
        assert approval.status == ApprovalStatus.STALE and approval.decision_by == "admin-demo"
    assert _registrations(session_factory, CODING) == 0
    assert "approval_invalidated" in _audit(session_factory, mission_id)

    # Still conflicting: re-selecting it is refused, and no new approval appears.
    assert _select(orchestrator, mission_id, CODING).result == SelectionResult.BLOCKED
    # Resolved: re-selecting re-checks it and asks for a NEW approval.
    _remove_exam(session_factory, exam_id)
    assert _select(orchestrator, mission_id, CODING).result == SelectionResult.SELECTED
    with session_factory() as session:
        chain = list_approvals_for_step(session, step_id)
    assert [a.status for a in chain] == [ApprovalStatus.STALE, ApprovalStatus.PENDING]
    assert _registrations(session_factory, CODING) == 0


# ---------------------------------------------------------------------------
# R: refreshing and selecting never call the LLM
# ---------------------------------------------------------------------------


def test_refresh_and_selection_make_no_llm_call(orchestrator, session_factory, llm) -> None:
    mission_id = _discover(orchestrator)
    calls_after_discovery = list(llm.calls)
    assert "plan_mission" in calls_after_discovery

    with session_factory() as session:
        refreshed = target_selection.refresh_candidates(session, mission_id=mission_id, student_id=STUDENT, now=utc_now())
    assert refreshed and all(c.assessment.schedule_source in (target_selection.SCHEDULE_CURRENT, None) for c in refreshed)
    _select(orchestrator, mission_id, CODING)
    step_id = next(iter(_mission(session_factory, mission_id)[2].values())).step_id
    _approve_latest(session_factory, step_id)
    orchestrator.resume_mission(mission_id)

    assert llm.calls == calls_after_discovery


# ---------------------------------------------------------------------------
# S (same process, fresh objects): state lives in the database
# ---------------------------------------------------------------------------


def test_candidates_and_selection_survive_fresh_orchestrator_and_sessions(session_factory, seeded_session, knowledge_service) -> None:
    from sqlalchemy.orm import sessionmaker

    first = _orchestrator(session_factory, knowledge_service, MockLLMProvider())
    mission_id = _discover(first)
    _select(first, mission_id, CODING)

    engine = session_factory.kw["bind"]
    fresh_factory = sessionmaker(bind=engine.engine, expire_on_commit=False)
    fresh = _orchestrator(fresh_factory, knowledge_service, MockLLMProvider())
    candidates = _candidates(fresh_factory, mission_id)
    assert candidates[CODING].selected and candidates[CODING].assessment.status == CandidateStatus.ELIGIBLE
    assert _select(fresh, mission_id, CODING).result == SelectionResult.ALREADY_SELECTED
    step_id = next(iter(_mission(fresh_factory, mission_id)[2].values())).step_id
    _approve_latest(fresh_factory, step_id)
    assert fresh.resume_mission(mission_id)["mission_status"] == MissionStatus.COMPLETED
    assert _registrations(fresh_factory, CODING) == 1


# ---------------------------------------------------------------------------
# T: the explicitly named flow is unchanged
# ---------------------------------------------------------------------------


def test_named_registration_flow_is_unchanged(orchestrator, session_factory) -> None:
    final = orchestrator.run_mission(NAMED_GOAL, user_id=STUDENT, user_role=UserRole.STUDENT, student_id=STUDENT)
    mission_id = final["mission_id"]

    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    action = next(t for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)
    assert final["agent_results"][action.task_id].facts["target_provenance"]["source"] == TargetSource.USER_GOAL.value
    assert _mission(session_factory, mission_id)[3] is False
    with session_factory() as session:
        rows = session.execute(select(ActionCandidateRecord).where(ActionCandidateRecord.mission_id == mission_id)).scalars().all()
    assert rows == []
    assert "action_candidates_recorded" not in _audit(session_factory, mission_id)


# ---------------------------------------------------------------------------
# A database created before Phase 13 is upgraded before the first mission
# ---------------------------------------------------------------------------


def test_orchestrator_upgrades_a_database_without_the_phase13_tables(session_factory, seeded_session, knowledge_service) -> None:
    from sqlalchemy import inspect, text

    engine = session_factory.kw["bind"]
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE target_selections"))
        conn.execute(text("DROP TABLE action_candidates"))
    assert "target_selections" not in inspect(engine).get_table_names()

    orchestrator = _orchestrator(session_factory, knowledge_service, MockLLMProvider())
    mission_id = _discover(orchestrator)

    assert {"target_selections", "action_candidates"} <= set(inspect(engine).get_table_names())
    assert _candidates(session_factory, mission_id)[CODING].selectable
