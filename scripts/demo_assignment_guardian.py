"""Local end-to-end Assignment Guardian run on a throwaway SQLite database (AgentOS V2 Phase 3).

Faculty creates an assignment for a 3-student class -> publish -> the Guardian mission exists ->
student 1 submits -> worker runs the Guardian -> 2 communication requests -> student 2 submits ->
worker (no AI call) -> student 3 submits -> worker verifies -> mission COMPLETED.

Offline (mock Guardian brain), pinned clock, nothing touches the dev database. Prints a structured
JSON trace only and exits non-zero if any expectation fails.

    python scripts/demo_assignment_guardian.py
"""
from __future__ import annotations

import json
import secrets
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.agentos.bootstrap import build_agent_runtime  # noqa: E402
from app.agentos.providers import MockAgentBrain  # noqa: E402
from app.agentos.runtime import MissionActor  # noqa: E402
from app.agentos.worker import process_due_missions  # noqa: E402
from app.auth.accounts import seed_dev_accounts  # noqa: E402
from app.auth.passwords import hash_password  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentMission, AgentStep, AuthAccount, Course, DomainEvent, Enrollment, FacultyProfile, OrganizationMembership, Student,
    TeachingAssignment, User,
)
from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from app.db.tenancy import ensure_default_organization  # noqa: E402
from app.db.tenant_session import TenantSessionFactory  # noqa: E402
from app.rules.assignment_rules import FollowupPolicy  # noqa: E402
from app.schemas.enums import UserRole  # noqa: E402
from app.services import assignments  # noqa: E402
from scripts.seed_data import run_seed  # noqa: E402

T0 = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)
POLICY = FollowupPolicy(quiet_start=time(0, 0), quiet_end=time(0, 0))  # quiet hours off: the trace is time-of-day independent
CODES = ("E2E-001", "E2E-002", "E2E-003")


def trace(step: str, **data) -> None:
    print(json.dumps({"step": step, **data}, default=str, separators=(",", ":")))


def build_class(system_session) -> tuple[int, dict, MissionActor]:
    org = ensure_default_organization(system_session).id
    account = system_session.execute(select(AuthAccount).where(AuthAccount.email == "faculty@campusnexus.local")).scalar_one()
    membership = system_session.execute(select(OrganizationMembership).where(
        OrganizationMembership.account_id == account.id)).scalar_one()
    faculty = system_session.get(FacultyProfile, membership.faculty_profile_id)
    course = Course(organization_id=org, code="E2E301", title="Guardian E2E", department_id=faculty.department_id,
                    credits=3, semester=5, instructor=faculty.full_name)
    system_session.add(course)
    system_session.flush()
    teaching = TeachingAssignment(organization_id=org, faculty_id=faculty.id, course_id=course.id,
                                  department_id=faculty.department_id, year=3, semester=5, section="E",
                                  academic_term="2026-2027")
    system_session.add(teaching)
    students = {}
    for code in CODES:
        user = User(organization_id=org, email=f"{code.lower()}@example.invalid", full_name=code, role=UserRole.STUDENT)
        system_session.add(user)
        system_session.flush()
        student = Student(organization_id=org, student_code=code, user_id=user.id, department_id=faculty.department_id,
                          year=3, semester=5, cgpa=7.0, section="E")
        system_session.add(student)
        system_session.flush()
        system_session.add(Enrollment(organization_id=org, student_id=student.id, course_id=course.id,
                                      academic_year="2026-2027", semester=5))
        login = AuthAccount(email=f"{code.lower()}@campusnexus.local", password_hash=hash_password(secrets.token_urlsafe(16)),
                            role=UserRole.STUDENT, display_name=code, linked_student_id=code)
        system_session.add(login)
        system_session.flush()
        system_session.add(OrganizationMembership(organization_id=org, account_id=login.id, role=UserRole.STUDENT,
                                                  student_id=student.id))
        students[code] = (student.id, login.id)
    system_session.commit()
    actor = MissionActor(org, account.id, membership.role, faculty_profile_id=faculty.id)
    return teaching.id, students, actor


def main() -> int:
    clock = {"now": T0}
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_db_engine(db_path=str(Path(tmp) / "guardian_e2e.db"))
        init_db(engine)
        with create_session_factory(engine)() as system_session:
            run_seed(system_session)
            seed_dev_accounts(system_session, secrets.token_urlsafe(16))
            teaching_id, students, faculty = build_class(system_session)
        factory = TenantSessionFactory(engine)
        runtime = build_agent_runtime(MockAgentBrain(), clock=lambda: clock["now"], guardian_policy=POLICY)
        org = faculty.organization_id

        def student_actor(code: str) -> MissionActor:
            return MissionActor(org, students[code][1], UserRole.STUDENT, student_code=code)

        def advance(minutes: int) -> None:
            clock["now"] += timedelta(minutes=minutes)

        def work(label: str) -> None:
            report = process_due_missions(factory, runtime, now=clock["now"], worker_id="e2e-worker")
            trace(label, runs=[r.model_dump() for r in report.runs])

        def mission_state(mission_id: int) -> dict:
            with factory.open_tenant_session(org) as s:
                m = s.get(AgentMission, mission_id)
                steps = list(s.execute(select(AgentStep.action_type, AgentStep.tool_name, AgentStep.status)
                                       .where(AgentStep.mission_id == mission_id).order_by(AgentStep.step_number)).all())
                return {"status": m.status.value, "waiting_for": m.waiting_for, "next_wake_at": m.next_wake_at,
                        "outcome": (m.context or {}).get("outcome"),
                        "steps": [f"{a}:{t}" if t else f"{a}" for a, t, _ in steps]}

        with factory.open_tenant_session(org) as s:
            created = assignments.create_assignment(s, faculty, assignments.CreateAssignment(
                teaching_assignment_id=teaching_id, title="Lab 3", deadline_at=T0 + timedelta(hours=20)), clock["now"])
            trace("assignment_created", assignment_id=created.id, status=created.status.value)
            result = assignments.publish(s, runtime, faculty, created.id, clock["now"])
        aid, mid = result.assignment.id, result.guardian_mission_id
        trace("published", assignment_id=aid, target_count=result.target_count, guardian_mission_id=mid,
              mission=mission_state(mid))

        def submit(code: str) -> None:
            with factory.open_tenant_session(org) as s:
                row = assignments.submit(s, student_actor(code), aid, assignments.SubmitAssignment(), clock["now"])
                trace("submitted", student_id=students[code][0], status=row.status.value)

        advance(1)
        submit("E2E-001")
        advance(1)
        work("worker_run_1")
        with factory.open_tenant_session(org) as s:
            requests = [e.payload for e in s.execute(select(DomainEvent).where(
                DomainEvent.event_type == "COMMUNICATION_REQUESTED").order_by(DomainEvent.id)).scalars()]
        trace("communication_requested", count=len(requests), payloads=requests, mission=mission_state(mid))

        advance(10)
        submit("E2E-002")
        advance(1)
        work("worker_run_2")
        trace("after_second_submission", mission=mission_state(mid))

        advance(10)
        submit("E2E-003")
        advance(1)
        work("worker_run_3")
        final = mission_state(mid)
        trace("final", mission=final)
        engine.dispose()

    ok = (len(requests) == 2 and final["status"] == "completed"
          and (final["outcome"] or {}).get("result") == "ALL_SUBMITTED")
    trace("result", ok=ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
