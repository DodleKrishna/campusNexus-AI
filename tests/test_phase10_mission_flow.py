"""Phase 10 -- mission & action flow refinement.

1. Registration missions feed verified Academic timetable/exam facts into the
   Action Agent *before* approval (planner shape + dependency propagation).
2. A conflict-free event reaches approval with a VERIFIED pre-check; a known
   conflict is blocked before any approval is created.
3. The execute-time recheck is still meaningful: a schedule change *after*
   approval (time-of-check / time-of-use) blocks the write.
4. Bounded replanning: an identical deterministic failure is not replanned
   again, a changed failure/input still is, and the audit trail records it.

Every test uses the isolated per-test DB/Chroma fixtures and the offline mock
LLM (or deterministic test doubles) -- no live LLM, never the dev DB.
"""
from __future__ import annotations

from sqlalchemy import select

from app.agents.action.agent import ActionAgent
from app.api.main import _build_registry
from app.db.models.academic import Enrollment, Exam
from app.db.models.events import Event, EventRegistration
from app.db.models.identity import Student
from app.db.repositories.missions import get_latest_approval_for_step
from app.graph.failures import all_failures_repeated, failure_fingerprint, normalize_reason
from app.graph.orchestrator import DUPLICATE_FAILURE_MESSAGE, MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.providers.mock import MockLLMProvider
from app.schemas.agent import AgentMessage
from app.schemas.enums import AgentName, ApprovalStatus, MissionStatus, TaskStatus, UserRole, VerificationStatus
from app.schemas.mission import MissionPlan, MissionTask
from app.services import academic as academic_service
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.tools.build import build_default_tool_registry
from app.verification.action import ActionVerifier
from app.schemas.verification import VerificationCheck
from tests.graph_doubles import AlwaysFailAgent, ChangingFailureAgent, FixedPlanLLMProvider, SuccessAgent

DEMO_STUDENT = "STU-DEMO-001"
CLEAN_EVENT_ID = 10  # Competitive Coding Contest -- conflict-free for STU-DEMO-001 at seed time
CONFLICT_EVENT_ID = 6  # Tech Talk: Cloud Native Systems -- overlaps one of STU-DEMO-001's classes
REGISTER_GOAL = (
    "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
CONFLICT_GOAL = (
    "Find the workshop titled 'Tech Talk: Cloud Native Systems', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
FULL_EVENT_GOAL = (
    "Find the workshop titled 'Startup Pitch Night', verify there are no conflicts with my classes or "
    "exams, and register me for it."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _production_orchestrator(session_factory, knowledge_service) -> MissionOrchestrator:
    """The same registry the FastAPI app uses (all five agents)."""
    llm = MockLLMProvider()
    registry = _build_registry(knowledge_service, llm, build_default_tool_registry())
    return MissionOrchestrator(session_factory=session_factory, registry=registry, llm_provider=llm)


def _run(orchestrator, goal):
    return orchestrator.run_mission(goal, user_id=DEMO_STUDENT, user_role=UserRole.STUDENT, student_id=DEMO_STUDENT)


def _action_task(final) -> MissionTask:
    return next(t for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT)


def _approve(session_factory, task_id: str) -> None:
    with session_factory() as session:
        approval = get_latest_approval_for_step(session, task_id)
        assert approval is not None and approval.status == ApprovalStatus.PENDING
        ApprovalGate(session).decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by="admin-demo")


def _registration_rows(session_factory, event_id: int) -> int:
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
        return len(
            session.execute(
                select(EventRegistration).where(
                    EventRegistration.event_id == event_id, EventRegistration.student_id == student.id
                )
            ).scalars().all()
        )


def _add_exam_over_event(session_factory, event_id: int) -> None:
    """An exam for one of the student's own courses rescheduled onto the event's slot."""
    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
        enrollment = session.execute(select(Enrollment).where(Enrollment.student_id == student.id)).scalars().first()
        event = session.get(Event, event_id)
        session.add(
            Exam(course_id=enrollment.course_id, exam_type="quiz", scheduled_start=event.start_at,
                 scheduled_end=event.end_at, location="Rescheduled Exam Hall")
        )
        session.commit()


def _audit(session_factory, mission_id: str):
    with session_factory() as session:
        return [(e.event_type, dict(e.event_metadata or {})) for e in ContextService(session).list_audit_events(mission_id)]


def _runs(session_factory, mission_id: str, agent: AgentName):
    with session_factory() as session:
        return [r for r in ContextService(session).list_agent_runs(mission_id) if r.agent == agent]


# ---------------------------------------------------------------------------
# 1. planner shape + dependency propagation
# ---------------------------------------------------------------------------


def test_registration_planner_includes_parallel_academic_dependencies() -> None:
    plan = MockLLMProvider().plan_mission("m", REGISTER_GOAL, supported_agents=list(AgentName))
    action = next(t for t in plan.tasks if t.agent == AgentName.ACTION_AGENT)
    upstream = [t for t in plan.tasks if t.task_id in action.dependencies]

    assert sorted(t.agent for t in upstream) == sorted(
        [AgentName.EVENTS_OPPORTUNITY_AGENT, AgentName.ACADEMIC_AGENT, AgentName.ACADEMIC_AGENT]
    )
    assert {t.objective for t in upstream if t.agent == AgentName.ACADEMIC_AGENT} == {
        "What is my timetable?", "When are my exams?"
    }
    # Independent of each other, so the scheduler runs them in parallel.
    assert all(t.dependencies == [] for t in upstream)
    assert action.constraints == {"tool_name": "register_event", "event_title": "Competitive Coding Contest"}


def test_registration_planner_without_academic_agent_does_not_invent_schedule_tasks() -> None:
    supported = [AgentName.EVENTS_OPPORTUNITY_AGENT, AgentName.ACTION_AGENT]
    plan = MockLLMProvider().plan_mission("m", REGISTER_GOAL, supported_agents=supported)
    assert [t.agent for t in plan.tasks] == [AgentName.EVENTS_OPPORTUNITY_AGENT, AgentName.ACTION_AGENT]


def test_action_task_runs_only_after_its_three_upstream_tasks_complete(
    seeded_session, session_factory, knowledge_service
) -> None:
    final = _run(_production_orchestrator(session_factory, knowledge_service), REGISTER_GOAL)
    action = _action_task(final)
    with session_factory() as session:
        steps = {s.step_id: s for s in ContextService(session).get_mission(final["mission_id"]).steps}
    upstream_started = [steps[d].started_at for d in action.dependencies]
    # The action task was dispatched only after every upstream task had completed.
    assert max(upstream_started) <= steps[action.task_id].started_at
    assert all(steps[d].status == TaskStatus.COMPLETED for d in action.dependencies)


def test_upstream_academic_facts_reach_the_action_agent(seeded_session, knowledge_service) -> None:
    """Driving the Action Agent directly with the facts the dispatcher would
    merge from verified timetable + exam tasks: the propose-time check uses
    exactly those facts (source recorded) and comes back VERIFIED."""
    timetable = academic_service.get_timetable(seeded_session, DEMO_STUDENT)
    exams = academic_service.get_exam_schedule(seeded_session, DEMO_STUDENT)
    context = ContextService(seeded_session)
    context.create_mission("m-p10-facts", DEMO_STUDENT, UserRole.STUDENT, "register")
    context.create_mission_step("t-1", "m-p10-facts", AgentName.ACTION_AGENT, "register")
    agent = ActionAgent(session=seeded_session, knowledge_service=knowledge_service, tool_gateway=build_default_tool_registry())
    message = AgentMessage(
        message_id="msg-1", mission_id="m-p10-facts", task_id="t-1", source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACTION_AGENT, objective="register",
        facts={
            "student_id": DEMO_STUDENT,
            "timetable": [t.model_dump(mode="json") for t in timetable], "timetable_scope": "all",
            "exams": [e.model_dump(mode="json") for e in exams], "exams_scope": "all",
        },
        constraints={"tool_name": "register_event", "event_title": "Competitive Coding Contest"},
    )
    outcome = agent.handle(message)

    facts = outcome.agent_result.facts
    assert facts["precheck_status"] == "verified"
    schedule = facts["proposal"]["supporting_facts"]["schedule_check"]
    assert schedule["performed"] is True and schedule["source"] == "upstream_academic_tasks"
    assert schedule["timetable_entries_checked"] == len(timetable)
    assert schedule["exam_entries_checked"] == len(exams)


def test_course_scoped_timetable_is_not_treated_as_the_whole_schedule(seeded_session, knowledge_service) -> None:
    timetable = academic_service.get_timetable(seeded_session, DEMO_STUDENT)
    context = ContextService(seeded_session)
    context.create_mission("m-p10-scope", DEMO_STUDENT, UserRole.STUDENT, "register")
    context.create_mission_step("t-1", "m-p10-scope", AgentName.ACTION_AGENT, "register")
    agent = ActionAgent(session=seeded_session, knowledge_service=knowledge_service, tool_gateway=build_default_tool_registry())
    message = AgentMessage(
        message_id="msg-1", mission_id="m-p10-scope", task_id="t-1", source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACTION_AGENT, objective="register",
        facts={
            "student_id": DEMO_STUDENT,
            "timetable": [t.model_dump(mode="json") for t in timetable[:1]], "timetable_scope": timetable[0].course_code,
            "exams": [], "exams_scope": "all",
        },
        constraints={"tool_name": "register_event", "event_title": "Competitive Coding Contest"},
    )
    outcome = agent.handle(message)
    assert outcome.agent_result.facts["precheck_status"] == "needs_review"
    assert "No timetable/exam data was available to check for schedule conflicts." in outcome.agent_result.errors


def test_action_agent_never_fetches_missing_academic_context_itself(seeded_session, knowledge_service) -> None:
    """No upstream timetable/exams -> honestly "not checked" (NEEDS_REVIEW),
    never silently filled in from the DB at propose time."""
    context = ContextService(seeded_session)
    context.create_mission("m-p10-none", DEMO_STUDENT, UserRole.STUDENT, "register")
    context.create_mission_step("t-1", "m-p10-none", AgentName.ACTION_AGENT, "register")
    agent = ActionAgent(session=seeded_session, knowledge_service=knowledge_service, tool_gateway=build_default_tool_registry())
    message = AgentMessage(
        message_id="msg-1", mission_id="m-p10-none", task_id="t-1", source=AgentName.MISSION_ORCHESTRATOR,
        target=AgentName.ACTION_AGENT, objective="register", facts={"student_id": DEMO_STUDENT},
        constraints={"tool_name": "register_event", "event_title": "Tech Talk: Cloud Native Systems"},
    )
    outcome = agent.handle(message)
    assert outcome.agent_result.facts["precheck_status"] == "needs_review"
    assert outcome.agent_result.facts["proposal"]["supporting_facts"]["schedule_check"]["performed"] is False


# ---------------------------------------------------------------------------
# 2. pre-approval verdicts
# ---------------------------------------------------------------------------


def test_conflict_free_event_reaches_approval_with_a_verified_precheck(seeded_session, session_factory, knowledge_service) -> None:
    final = _run(_production_orchestrator(session_factory, knowledge_service), REGISTER_GOAL)
    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    action = _action_task(final)
    result = final["agent_results"][action.task_id]
    assert result.facts["precheck_status"] == "verified"
    assert result.errors == []  # no pre-check issue for the approver
    with session_factory() as session:
        assert get_latest_approval_for_step(session, action.task_id).status == ApprovalStatus.PENDING


def test_known_conflicting_event_is_blocked_before_approval(seeded_session, session_factory, knowledge_service) -> None:
    final = _run(_production_orchestrator(session_factory, knowledge_service), CONFLICT_GOAL)
    action = _action_task(final)
    result = final["agent_results"][action.task_id]

    assert final["mission_status"] == MissionStatus.FAILED
    assert result.facts["precheck_status"] == "failed"
    assert result.facts["schedule_check"]["timetable_conflicts"] or result.facts["schedule_check"]["exam_conflicts"]
    assert "clashes with your" in final["responses"][action.task_id]
    with session_factory() as session:
        assert get_latest_approval_for_step(session, action.task_id) is None  # never presented as approvable
    assert _registration_rows(session_factory, CONFLICT_EVENT_ID) == 0


def test_found_conflict_is_hard_only_when_explicitly_escalated() -> None:
    checks = [VerificationCheck(name="no_schedule_conflicts", passed=False, detail="1 schedule conflict(s) found.")]
    verifier = ActionVerifier()
    default = verifier.verify_pre_action(mission_id="m", task_id="t", checks=checks)
    escalated = verifier.verify_pre_action(
        mission_id="m", task_id="t", checks=checks, blocking_check_names=frozenset({"no_schedule_conflicts"})
    )
    assert default.status == VerificationStatus.NEEDS_REVIEW
    assert escalated.status == VerificationStatus.FAILED


# ---------------------------------------------------------------------------
# 3. execute-time recheck + TOCTOU
# ---------------------------------------------------------------------------


def test_execution_time_recheck_still_runs_after_a_verified_precheck(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _production_orchestrator(session_factory, knowledge_service)
    final = _run(orchestrator, REGISTER_GOAL)
    action = _action_task(final)
    _approve(session_factory, action.task_id)

    final2 = orchestrator.resume_mission(final["mission_id"])
    facts = final2["agent_results"][action.task_id].facts
    assert final2["mission_status"] == MissionStatus.COMPLETED
    assert facts["execution_recheck_status"] == "verified"
    assert facts["postcondition_verified"] is True
    assert _registration_rows(session_factory, CLEAN_EVENT_ID) == 1


def test_schedule_change_after_approval_blocks_the_write_toctou(seeded_session, session_factory, knowledge_service) -> None:
    orchestrator = _production_orchestrator(session_factory, knowledge_service)

    # 1-2. Conflict-free when proposed; the pre-check is VERIFIED.
    final = _run(orchestrator, REGISTER_GOAL)
    action = _action_task(final)
    assert final["mission_status"] == MissionStatus.NEEDS_APPROVAL
    assert final["agent_results"][action.task_id].facts["precheck_status"] == "verified"

    # 3. A human approves it.
    _approve(session_factory, action.task_id)

    # 4. The academic schedule changes before execution.
    _add_exam_over_event(session_factory, CLEAN_EVENT_ID)

    # 5-6. The execution-time recheck finds the new conflict and blocks the action.
    final2 = orchestrator.resume_mission(final["mission_id"])
    result = final2["agent_results"][action.task_id]
    assert final2["mission_status"] == MissionStatus.FAILED
    assert result.facts["execution_recheck_status"] == "failed"
    assert any("no longer valid" in e and "schedule conflict" in e for e in result.errors)

    # 7. No registration row was created.
    assert _registration_rows(session_factory, CLEAN_EVENT_ID) == 0


# ---------------------------------------------------------------------------
# 4. bounded replanning
# ---------------------------------------------------------------------------


def _single_task_orchestrator(session_factory, agent_cls, max_replans=2) -> MissionOrchestrator:
    registry = AgentRegistry()
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: agent_cls(s))

    def plan_factory(mission_id, goal):
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[
            MissionTask(task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT, objective="deterministic task")
        ])

    return MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=FixedPlanLLMProvider(plan_factory=plan_factory),
        max_replans=max_replans,
    )


def test_repeated_deterministic_failure_stops_replanning(session_factory) -> None:
    orchestrator = _single_task_orchestrator(session_factory, AlwaysFailAgent)
    final = orchestrator.run_mission("fails the same way", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.FAILED
    assert final["replan_count"] == 1  # original + one replan, not max_replans
    assert final["duplicate_failure_stop"] is True
    assert len(_runs(session_factory, final["mission_id"], AgentName.ACADEMIC_AGENT)) == 2
    assert final["final_result"].startswith(DUPLICATE_FAILURE_MESSAGE)


def test_changed_failure_permits_a_meaningful_retry(session_factory) -> None:
    orchestrator = _single_task_orchestrator(session_factory, ChangingFailureAgent)
    final = orchestrator.run_mission("fails differently", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.FAILED
    assert final["replan_count"] == 2  # bounded by max_replans only
    assert not final.get("duplicate_failure_stop")
    assert "duplicate_failure_detected" not in [t for t, _ in _audit(session_factory, final["mission_id"])]


def test_changed_upstream_context_permits_a_meaningful_retry(session_factory) -> None:
    """Same failing task, same reason -- but a replan gave it a different
    upstream task, so it was dispatched with different input facts."""
    calls = {"n": 0}

    def plan_factory(mission_id, goal):
        calls["n"] += 1
        upstream = MissionTask(
            task_id=f"{mission_id}-up-{calls['n']}", mission_id=mission_id, agent=AgentName.CAREER_AGENT,
            objective=f"fresh upstream data {calls['n']}",
        )
        failing = MissionTask(
            task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.ACADEMIC_AGENT,
            objective="deterministic task", dependencies=[upstream.task_id],
        )
        return MissionPlan(mission_id=mission_id, goal=goal, tasks=[upstream, failing])

    registry = AgentRegistry()
    registry.register(AgentName.CAREER_AGENT, lambda s: SuccessAgent(s))
    registry.register(AgentName.ACADEMIC_AGENT, lambda s: AlwaysFailAgent(s))
    orchestrator = MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=FixedPlanLLMProvider(plan_factory=plan_factory)
    )
    final = orchestrator.run_mission("retry with new context", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT)
    assert final["replan_count"] == 2
    assert not final.get("duplicate_failure_stop")


def test_audit_trail_records_the_duplicate_failure_prevention(session_factory) -> None:
    orchestrator = _single_task_orchestrator(session_factory, AlwaysFailAgent)
    final = orchestrator.run_mission("fails the same way", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT)
    audit = _audit(session_factory, final["mission_id"])
    types = [t for t, _ in audit]

    failures = [meta for t, meta in audit if t == "task_verified"]
    assert len(failures) == 2
    assert failures[0]["repeat_of_earlier_failure"] is False  # the original failure
    assert failures[1]["repeat_of_earlier_failure"] is True
    assert failures[0]["failure_fingerprint"] == failures[1]["failure_fingerprint"]

    replan = next(meta for t, meta in audit if t == "replan_triggered")  # the replan attempt
    assert list(replan["failed_tasks"].values()) == [failures[0]["failure_fingerprint"]]

    duplicate = next(meta for t, meta in audit if t == "duplicate_failure_detected")
    assert duplicate["task_ids"] == [f"{final['mission_id']}-task-1"]
    assert duplicate["replan_count"] == 1 and duplicate["max_replans"] == 2

    # Order: original failure -> replan -> repeat -> duplicate detection -> final status.
    assert types.index("replan_triggered") < types.index("duplicate_failure_detected") < types.index("mission_finalized")
    with session_factory() as session:
        assert ContextService(session).get_mission(final["mission_id"]).status == MissionStatus.FAILED


def test_resumed_mission_remembers_failures_recorded_by_an_earlier_process(session_factory) -> None:
    first = _single_task_orchestrator(session_factory, AlwaysFailAgent, max_replans=0)
    final = first.run_mission("fails", user_id=DEMO_STUDENT, user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED

    # A brand-new orchestrator (fresh process state) resumes it: the rebuilt
    # fingerprint history recognises the very next identical failure.
    second = _single_task_orchestrator(session_factory, AlwaysFailAgent, max_replans=2)
    resumed = second.resume_mission(final["mission_id"])
    assert resumed["duplicate_failure_stop"] is True
    assert resumed["replan_count"] == 1


def test_registration_with_deterministic_precheck_failure_runs_at_most_twice(
    seeded_session, session_factory, knowledge_service
) -> None:
    final = _run(_production_orchestrator(session_factory, knowledge_service), FULL_EVENT_GOAL)
    assert final["mission_status"] == MissionStatus.FAILED
    assert len(_runs(session_factory, final["mission_id"], AgentName.ACTION_AGENT)) == 2
    # The completed Academic/Events tasks are not re-run by the replan.
    assert len(_runs(session_factory, final["mission_id"], AgentName.ACADEMIC_AGENT)) == 2


# ---------------------------------------------------------------------------
# app/graph/failures.py (pure)
# ---------------------------------------------------------------------------


def _task(**overrides) -> MissionTask:
    fields = dict(task_id="m-1-task-1", mission_id="m-1", agent=AgentName.ACTION_AGENT, objective="Register for 'X'",
                  constraints={"tool_name": "register_event", "event_title": "X"})
    fields.update(overrides)
    return MissionTask(**fields)


def test_fingerprint_ignores_generated_ids_but_not_reasons_or_input() -> None:
    base = dict(mission_id="m-1", verification_status="failed", input_facts={"student_id": "S1"})
    a = failure_fingerprint(_task(), reasons=["tool call tc-0123456789ab failed: Event is at capacity."], **base)
    b = failure_fingerprint(_task(), reasons=["tool call tc-ba9876543210 failed: Event is at capacity."], **base)
    assert a == b
    assert a != failure_fingerprint(_task(), reasons=["The registration deadline has passed."], **base)
    assert a != failure_fingerprint(
        _task(), reasons=["tool call tc-0123456789ab failed: Event is at capacity."],
        mission_id="m-1", verification_status="failed", input_facts={"student_id": "S1", "timetable": []},
    )
    assert a != failure_fingerprint(
        _task(constraints={"tool_name": "register_event", "event_title": "Y"}),
        reasons=["tool call tc-0123456789ab failed: Event is at capacity."], **base,
    )


def test_normalize_reason_and_all_failures_repeated() -> None:
    assert normalize_reason("Task mission-0123456789ab-task-1  FAILED", mission_id="mission-0123456789ab") == "task <mission>-task-1 failed"
    assert all_failures_repeated(["t1"], ["t1"]) is True
    assert all_failures_repeated(["t1", "t2"], ["t1"]) is False
    assert all_failures_repeated([], []) is False
