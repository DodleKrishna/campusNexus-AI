"""AgentOS V2 Phase 5: source adapters, communication jobs, the Communication Agent, the in-app connector and the
communication worker.

Offline and deterministic: the clock is pinned, the brain is the offline mock policy or a ``ScriptedAgentBrain``,
the only connector that delivers is the real in-app one (or a test double of it), and nothing calls a provider.
"""
from __future__ import annotations

import ast
import json
from datetime import time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agentos.bootstrap import build_agent_runtime
from app.agentos.brain import ScriptedAgentBrain
from app.agentos.events import EventService
from app.agentos.providers import MockAgentBrain
from app.agentos.schemas import AgentDecision, DecisionKind, DomainEventType
from app.agentos.worker import _owner_actor
from app.communication import service as comm_service
from app.communication.base import DeliveryResult
from app.communication.connectors import ConnectorRegistry, InAppConnector, compose_message
from app.communication.contacts import ContactAccessDenied, ContactResolver
from app.communication.sources import (
    FOLLOWUP_NOT_FOUND, SOURCE_RECORD_MISSING, AssignmentSource, AttendanceSource, ExamSource, SourceFacts,
    SourceUnavailable, build_adapters,
)
from app.communication.worker import claim_job, process_communication_jobs
from app.db.models import (
    AgentMission, AgentMissionStatus, AgentStep, AgentStepStatus, Assignment, AssignmentFollowup, AttemptStatus,
    CommunicationAttempt, CommunicationJob, CommunicationJobStatus, CommunicationPreference, ContactKind, ContactPoint,
    DomainEvent, ExamFollowup, FollowupStatus, Notification, OperationAuditEvent,
)
from app.db.tenant_session import TenantSessionFactory
from app.rules import assignment_rules
from app.rules import communication_policy as cp
from app.rules.assignment_rules import FollowupPolicy
from tests.test_agentos_assignment_guardian import (  # noqa: F401 -- fixtures and helpers
    POLICY, T0, Clock, _request, clock, mission_of, published, rows, run_worker, small_class, submit,
)
from tests.test_agentos_exam_attendance_guardians import (
    ATT_POLICY, EXAM_POLICY, NO_QUIET, START, _exam_request, scheduled_exam,
)
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_FACULTY, PASSWORD, app, bearer, client, login, orgs,
)

COMM_POLICY = cp.CommunicationPolicy(max_attempts=2, backoff=timedelta(minutes=30), contact=FollowupPolicy(**NO_QUIET))
PHONE = "+91 98765 43210"
AGENT = "communication_agent"


def use_runtime(app, brain=None, connectors=None, policy=COMM_POLICY):
    app.state.agent_runtime = build_agent_runtime(
        brain or MockAgentBrain(), clock=lambda: app.state.clock(), guardian_policy=POLICY, exam_policy=EXAM_POLICY,
        attendance_policy=ATT_POLICY, communication_policy=policy, connectors=connectors or ConnectorRegistry())
    return app.state.agent_runtime


@pytest.fixture()
def runtime(app, clock):
    return use_runtime(app)


def tenant(session_factory, org):
    return TenantSessionFactory.from_sessionmaker(session_factory).open_tenant_session(org)


def comm(app, clock, **kwargs):
    return process_communication_jobs(app.state.session_factory, app.state.agent_runtime, now=clock.now, **kwargs)


def advance(app, org, mission_id, transitions=5):
    """Run the Communication Agent mission directly (as the worker would, under its owner's identity)."""
    with tenant(app.state.session_factory, org) as s:
        actor = _owner_actor(s, org, s.get(AgentMission, mission_id))
        return app.state.agent_runtime.run_until_blocked(s, actor, mission_id, max_transitions=transitions)


def requested(client, small_class, session_factory, orgs, codes=("GRD-001",)):
    """A published assignment (deadline T0+20h) with one follow-up request per code, made at T0+1m."""
    result = published(client, small_class)
    aid, mid = result["assignment"]["id"], result["guardian_mission_id"]
    out = _request(session_factory, orgs["a"], mid, aid, [small_class["students"][c] for c in codes],
                   T0 + timedelta(minutes=1))
    assert all(r["result"] == "requested" for r in out), out
    return aid, mid


def jobs(session_factory, *where):
    return rows(session_factory, CommunicationJob, *where)


def notifications(session_factory, student_id):
    return rows(session_factory, Notification, Notification.student_id == student_id)


def ready_job(app, client, clock, small_class, session_factory, orgs):
    """Intake + the agent choosing in-app: one READY job for GRD-001."""
    requested(client, small_class, session_factory, orgs)
    comm(app, clock)
    (job,) = jobs(session_factory)
    advance(app, orgs["a"], job.agent_mission_id)
    (job,) = jobs(session_factory)
    assert (job.status, job.selected_channel) == (CommunicationJobStatus.READY, "in_app")
    return job


# --- Source adapters -------------------------------------------------------------------------------------------------


def test_adapters_fail_safely_on_missing_linked_records(client, small_class, runtime, clock, session_factory, orgs) -> None:
    adapters = build_adapters(EXAM_POLICY, ATT_POLICY)
    missing = {
        "assignment": SimpleNamespace(status=FollowupStatus.REQUESTED, assignment_id=999999, student_id=1),
        "exam": SimpleNamespace(status=FollowupStatus.REQUESTED, exam_id=999999, student_id=1, purpose="exam_reminder"),
        "attendance": SimpleNamespace(status=FollowupStatus.REQUESTED, intervention_id=999999, student_id=1),
    }
    with tenant(session_factory, orgs["a"]) as s:
        for source_type, followup in missing.items():
            adapter = adapters[source_type]
            for call in (lambda: adapter.facts(s, followup), lambda: adapter.closed_reason(s, followup, T0)):
                with pytest.raises(SourceUnavailable) as exc:
                    call()
                assert exc.value.code == SOURCE_RECORD_MISSING
            assert adapter.is_open(s, followup, T0) is False
            with pytest.raises(SourceUnavailable) as exc:
                adapter.load(s, 999999)
            assert exc.value.code == FOLLOWUP_NOT_FOUND


def test_another_organizations_follow_up_behaves_as_missing(client, small_class, runtime, clock, session_factory,
                                                            orgs) -> None:
    requested(client, small_class, session_factory, orgs)
    (followup,) = rows(session_factory, AssignmentFollowup)
    with tenant(session_factory, orgs["b"]) as s:
        with pytest.raises(SourceUnavailable) as exc:
            AssignmentSource().load(s, followup.id)
        assert exc.value.code == FOLLOWUP_NOT_FOUND
        EventService().publish(s, event_type=DomainEventType.COMMUNICATION_REQUESTED, subject_type="assignment_followup",
                               subject_id=followup.id, now=T0, payload={"followup_id": followup.id})
        s.commit()
    report = comm(client.app, clock)
    assert sorted((r.result, r.code) for r in report.intake) == [("created", None), ("rejected", FOLLOWUP_NOT_FOUND)]
    (job,) = jobs(session_factory)
    assert job.organization_id == orgs["a"] and job.source_followup_id == followup.id


def test_assignment_adapter_matches_the_phase3_deadline_rule(client, small_class, runtime, clock, session_factory,
                                                             orgs) -> None:
    aid, _ = requested(client, small_class, session_factory, orgs)
    with tenant(session_factory, orgs["a"]) as s:
        assignment = s.get(Assignment, aid)
        followup = AssignmentSource().load(s, rows(session_factory, AssignmentFollowup)[0].id)
        deadline = assignment.deadline_at
        for now, expected in ((deadline, None), (deadline + timedelta(microseconds=1), "DEADLINE_PASSED")):
            assert AssignmentSource().closed_reason(s, followup, now) == expected
            refusal = assignment_rules.open_followup_refusal(now=now, assignment_status=assignment.status,
                                                             deadline_at=deadline, has_submitted=False)
            assert (refusal.value if refusal else None) == expected
        facts = AssignmentSource().facts(s, followup)
    assert facts == SourceFacts("assignment", "Lab 3", "GRD301", deadline)
    assert set(vars(facts)) == {"kind", "title", "course_code", "when"}  # never a name or contact detail


def test_exam_adapter_uses_the_exam_rule(client, small_class, runtime, clock, session_factory, orgs) -> None:
    result = scheduled_exam(client, small_class)
    eid, mid = result["exam"]["id"], result["guardian_mission_id"]
    _exam_request(session_factory, orgs["a"], mid, eid, [small_class["students"]["GRD-001"]], "exam_reminder",
                  T0 + timedelta(hours=7))
    (followup,) = rows(session_factory, ExamFollowup)
    adapter = ExamSource(EXAM_POLICY)
    with tenant(session_factory, orgs["a"]) as s:
        row = adapter.load(s, followup.id)
        assert adapter.closed_reason(s, row, START - timedelta(microseconds=1)) is None
        assert adapter.closed_reason(s, row, START) == "EXAM_ALREADY_STARTED"
        assert adapter.facts(s, row).course_code == "GRD301"


def test_attendance_adapter_requires_the_class_session(client, small_class, runtime, clock, session_factory, orgs) -> None:
    with tenant(session_factory, orgs["a"]) as s:
        with pytest.raises(SourceUnavailable):
            AttendanceSource(ATT_POLICY).closed_reason(
                s, SimpleNamespace(status=FollowupStatus.REQUESTED, intervention_id=999999, student_id=1), T0)
        closed = SimpleNamespace(status=FollowupStatus.DELIVERED, intervention_id=999999, student_id=1)
        assert AttendanceSource(ATT_POLICY).closed_reason(s, closed, T0) == "FOLLOWUP_CLOSED"


# --- Intake ----------------------------------------------------------------------------------------------------------


def test_one_job_per_follow_up_idempotently(client, small_class, runtime, clock, session_factory, orgs) -> None:
    requested(client, small_class, session_factory, orgs, codes=("GRD-001", "GRD-002"))
    first = comm(client.app, clock)
    assert [r.result for r in first.intake] == ["created", "created"]
    (event,) = rows(session_factory, DomainEvent, DomainEvent.event_type == "COMMUNICATION_REQUESTED",
                    DomainEvent.subject_id == str(rows(session_factory, AssignmentFollowup)[0].id))
    with session_factory() as s:  # the same request delivered twice (a replayed event)
        s.get(DomainEvent, event.id).consumed_at = None
        s.commit()
    second = comm(client.app, clock)
    assert [(r.result, r.job_id) for r in second.intake] == [("duplicate", first.intake[0].job_id)]
    assert comm(client.app, clock).intake == []
    created = jobs(session_factory)
    assert len(created) == 2 and len({j.agent_mission_id for j in created}) == 2
    followups = rows(session_factory, AssignmentFollowup)
    assert {f.communication_job_id for f in followups} == {j.id for j in created}
    job = created[0]
    assert (job.status, job.authorization.value, job.purpose, job.max_attempts) == (
        CommunicationJobStatus.PENDING, "standing_policy", "submission_reminder", 2)
    mission = rows(session_factory, AgentMission, AgentMission.id == job.agent_mission_id)[0]
    assert (mission.agent_key, mission.context["inputs"]) == (AGENT, {"job_id": job.id})


def test_a_resolved_source_is_rejected_at_intake(client, small_class, runtime, clock, session_factory, orgs) -> None:
    aid, _ = requested(client, small_class, session_factory, orgs)
    assert submit(client, "GRD-001", aid).status_code in (200, 201)
    report = comm(client.app, clock)
    assert [r.result for r in report.intake] == ["rejected"]
    assert report.intake[0].code in ("FOLLOWUP_CLOSED", "ALREADY_SUBMITTED")
    assert jobs(session_factory) == []
    (followup,) = rows(session_factory, AssignmentFollowup)
    assert followup.status == FollowupStatus.CANCELLED


# --- End to end: the real in-app connector ------------------------------------------------------------------------------


def test_in_app_delivery_end_to_end(client, small_class, runtime, clock, session_factory, orgs) -> None:
    job = ready_job(client.app, client, clock, small_class, session_factory, orgs)
    mission, steps = mission_of(session_factory, job.agent_mission_id)
    assert [s.tool_name for s in steps if s.tool_name] == ["get_allowed_channels", "request_delivery"]
    assert mission.status == AgentMissionStatus.WAITING_EVENT

    report = comm(client.app, clock)
    assert [(r.outcome, r.code) for r in report.runs] == [("delivered", "DELIVERED_IN_APP")]
    student = small_class["students"]["GRD-001"]
    (note,) = notifications(session_factory, student)
    assert note.category == "communication:submission_reminder" and "Lab 3" in note.body and "GRD301" in note.body
    (job,) = jobs(session_factory)
    (attempt,) = rows(session_factory, CommunicationAttempt)
    (followup,) = rows(session_factory, AssignmentFollowup)
    assert (job.status, job.attempt_count, job.outcome_code) == (CommunicationJobStatus.DELIVERED, 1, "DELIVERED_IN_APP")
    assert (attempt.status, attempt.provider_reference) == (AttemptStatus.DELIVERED, f"notification:{note.id}")
    assert (followup.status, followup.outcome_code, followup.communication_job_id) == (
        FollowupStatus.DELIVERED, "DELIVERED_IN_APP", job.id)
    assert followup.delivered_at is not None
    guardian_events = rows(session_factory, DomainEvent, DomainEvent.subject_id == str(job.source_mission_id),
                           DomainEvent.event_type == "COMMUNICATION_DELIVERED")
    assert len(guardian_events) == 1

    advance(client.app, orgs["a"], job.agent_mission_id)
    mission, steps = mission_of(session_factory, job.agent_mission_id)
    assert mission.status == AgentMissionStatus.COMPLETED and steps[-1].action_type == "verified_complete"
    assert mission.context["outcome"]["result"] == "DELIVERED_IN_APP"
    assert comm(client.app, clock).runs == []  # nothing is delivered twice
    assert len(notifications(session_factory, student)) == 1


def test_worker_drives_guardian_agent_and_delivery_together(client, small_class, runtime, clock, session_factory) -> None:
    published(client, small_class)
    clock.advance(minutes=1)
    run_worker(app=client.app)  # the Assignment Guardian requests three follow-ups
    assert len(comm(client.app, clock).intake) == 3
    run_worker(app=client.app)  # three Communication Agents choose in-app
    assert [r.outcome for r in comm(client.app, clock).runs] == ["delivered"] * 3
    run_worker(app=client.app)  # and verify their deliveries
    statuses = [m.status for m in rows(session_factory, AgentMission, AgentMission.agent_key == AGENT)]
    assert statuses == [AgentMissionStatus.COMPLETED] * 3
    assert {f.status for f in rows(session_factory, AssignmentFollowup)} == {FollowupStatus.DELIVERED}


def test_resolved_before_delivery_cancels_without_contact(client, small_class, runtime, clock, session_factory,
                                                          orgs) -> None:
    job = ready_job(client.app, client, clock, small_class, session_factory, orgs)
    assert submit(client, "GRD-001", job.source_id).status_code in (200, 201)
    report = comm(client.app, clock)
    assert [r.outcome for r in report.runs] == ["cancelled"]
    (job,) = jobs(session_factory)
    assert job.status == CommunicationJobStatus.CANCELLED and rows(session_factory, CommunicationAttempt) == []
    assert notifications(session_factory, small_class["students"]["GRD-001"]) == []
    advance(client.app, orgs["a"], job.agent_mission_id)
    mission, steps = mission_of(session_factory, job.agent_mission_id)
    assert mission.status == AgentMissionStatus.CANCELLED and steps[-1].action_type == "verified_cancel"


# --- The agent cannot bypass policy ------------------------------------------------------------------------------------


def test_the_agent_cannot_choose_an_unconsented_channel_or_complete_early(client, small_class, app, clock,
                                                                          session_factory, orgs) -> None:
    brain = ScriptedAgentBrain([
        AgentDecision(kind=DecisionKind.TOOL, tool_name="request_delivery", tool_input={"channel": "voice"}),
        AgentDecision(kind=DecisionKind.COMPLETE, outcome="Sent!"),
    ])
    use_runtime(app, brain)
    requested(client, small_class, session_factory, orgs)
    comm(app, clock)
    (job,) = jobs(session_factory)
    advance(app, orgs["a"], job.agent_mission_id, transitions=2)
    mission, steps = mission_of(session_factory, job.agent_mission_id)
    assert (steps[0].status, steps[0].output_summary["error_code"]) == (AgentStepStatus.FAILED, "CHANNEL_NOT_CONSENTED")
    assert (steps[1].status, steps[1].output_summary["error_code"]) == (AgentStepStatus.REJECTED, "COMPLETION_NOT_VERIFIED")
    assert mission.status == AgentMissionStatus.WAITING_EVENT
    (job,) = jobs(session_factory)
    assert (job.status, job.selected_channel) == (CommunicationJobStatus.PENDING, None)
    assert comm(app, clock).runs == [] and rows(session_factory, Notification, Notification.category.like("communication:%")) == []
    assert "voice" not in brain.seen[0].state["supervisor"]["permitted_channels"]


def test_no_permitted_channel_fails_deterministically(client, small_class, runtime, clock, session_factory, orgs) -> None:
    requested(client, small_class, session_factory, orgs)
    with tenant(session_factory, orgs["a"]) as s:
        s.add(CommunicationPreference(student_id=small_class["students"]["GRD-001"], allow_in_app=False))
        s.commit()
    comm(client.app, clock)
    (job,) = jobs(session_factory)
    advance(client.app, orgs["a"], job.agent_mission_id)
    mission, steps = mission_of(session_factory, job.agent_mission_id)
    assert mission.status == AgentMissionStatus.FAILED and mission.context["outcome"]["result"] == "NO_PERMITTED_CHANNEL"
    (job,) = jobs(session_factory)
    (followup,) = rows(session_factory, AssignmentFollowup)
    assert (job.status, followup.status) == (CommunicationJobStatus.FAILED, FollowupStatus.FAILED)


def test_consent_revoked_after_choice_returns_the_job_to_the_agent(client, small_class, runtime, clock, session_factory,
                                                                    orgs) -> None:
    job = ready_job(client.app, client, clock, small_class, session_factory, orgs)
    with tenant(session_factory, orgs["a"]) as s:
        s.add(CommunicationPreference(student_id=job.recipient_student_id, allow_in_app=False))
        s.commit()
    assert [(r.outcome, r.code) for r in comm(client.app, clock).runs] == [("returned_to_agent", "CHANNEL_NOT_CONSENTED")]
    (job,) = jobs(session_factory)
    assert (job.status, job.attempt_count) == (CommunicationJobStatus.PENDING, 0)
    assert notifications(session_factory, job.recipient_student_id) == []


# --- Failures, retries and the post-condition check --------------------------------------------------------------------


class FakeInApp(InAppConnector):
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def deliver(self, session, request):
        if self.mode == "crash":
            super().deliver(session, request)  # stages a notification, then the provider blows up
            raise RuntimeError("boom")
        if self.mode == "lie":
            return DeliveryResult("delivered", provider_reference="notification:999999", outcome_code="DELIVERED_IN_APP")
        return DeliveryResult("failed", error_code="PROVIDER_UNAVAILABLE")


@pytest.mark.parametrize("mode", ["fail", "crash"])
def test_retryable_failures_back_off_then_fail_the_job(client, small_class, app, clock, session_factory, orgs, mode) -> None:
    use_runtime(app, connectors=ConnectorRegistry([FakeInApp(mode)]))
    job = ready_job(app, client, clock, small_class, session_factory, orgs)
    assert [(r.outcome, r.code) for r in comm(app, clock).runs] == [("retry_scheduled", "PROVIDER_UNAVAILABLE")]
    (job,) = jobs(session_factory)
    assert (job.status, job.next_attempt_at) == (CommunicationJobStatus.DEFERRED, clock.now + timedelta(minutes=30))
    clock.advance(minutes=29)
    assert comm(app, clock).runs == []  # backoff respected
    clock.advance(minutes=1)
    assert [r.outcome for r in comm(app, clock).runs] == ["failed"]
    (job,) = jobs(session_factory)
    (followup,) = rows(session_factory, AssignmentFollowup)
    assert (job.status, job.outcome_code, job.attempt_count) == (CommunicationJobStatus.FAILED, "MAX_ATTEMPTS_REACHED", 2)
    assert followup.status == FollowupStatus.FAILED
    assert [a.status for a in rows(session_factory, CommunicationAttempt)] == [AttemptStatus.FAILED] * 2
    assert notifications(session_factory, job.recipient_student_id) == []  # a crashed connector leaves nothing behind
    advance(app, orgs["a"], job.agent_mission_id)
    mission, _ = mission_of(session_factory, job.agent_mission_id)
    assert mission.status == AgentMissionStatus.FAILED and mission.context["outcome"]["result"] == "MAX_ATTEMPTS_REACHED"


def test_an_unverified_delivery_is_never_counted(client, small_class, app, clock, session_factory, orgs) -> None:
    use_runtime(app, connectors=ConnectorRegistry([FakeInApp("lie")]))
    ready_job(app, client, clock, small_class, session_factory, orgs)
    assert [(r.outcome, r.code) for r in comm(app, clock).runs] == [("failed", "POSTCONDITION_FAILED")]
    (job,) = jobs(session_factory)
    (followup,) = rows(session_factory, AssignmentFollowup)
    assert (job.status, followup.status) == (CommunicationJobStatus.FAILED, FollowupStatus.FAILED)


def test_a_job_is_claimed_by_one_worker_only(client, small_class, runtime, clock, session_factory, orgs) -> None:
    job = ready_job(client.app, client, clock, small_class, session_factory, orgs)
    with tenant(session_factory, orgs["a"]) as s:
        assert claim_job(s, job.id, "worker-1", clock.now) is True
        assert claim_job(s, job.id, "worker-2", clock.now) is False
    assert [r.outcome for r in comm(client.app, clock).runs] == []  # leased: not even listed as due
    clock.advance(minutes=6)  # the lease expired
    assert [r.outcome for r in comm(client.app, clock).runs] == ["delivered"]


def test_worker_is_bounded(client, small_class, runtime, clock, session_factory, orgs) -> None:
    requested(client, small_class, session_factory, orgs, codes=("GRD-001", "GRD-002", "GRD-003"))
    with pytest.raises(ValueError):
        comm(client.app, clock, limit=0)
    assert len(comm(client.app, clock, limit=2).intake) == 2
    assert len(comm(client.app, clock, limit=2).intake) == 1


# --- Contact details never leave the connector layer ---------------------------------------------------------------------


def test_contact_resolver_is_connector_only_and_masked(client, small_class, runtime, clock, session_factory, orgs) -> None:
    student = small_class["students"]["GRD-001"]
    with tenant(session_factory, orgs["a"]) as s:
        s.add(ContactPoint(student_id=student, kind=ContactKind.PHONE, address=PHONE, verified=True))
        s.commit()
        resolver = ContactResolver()
        with pytest.raises(ContactAccessDenied):
            resolver.resolve(s, object(), student, ContactKind.PHONE)
        contact = resolver.resolve(s, InAppConnector(), student, ContactKind.PHONE)
        assert contact.address == PHONE and "98765" not in repr(contact) and "98765" not in str(contact)
        assert resolver.resolve(s, InAppConnector(), student, ContactKind.EMAIL).address == "grd-001@meridian.edu"
    with tenant(session_factory, orgs["b"]) as s:  # another organization's contact point is invisible
        assert ContactResolver().resolve(s, InAppConnector(), student, ContactKind.PHONE) is None


def test_only_the_connector_layer_imports_contact_details() -> None:
    root = Path(__file__).resolve().parents[1] / "app"
    allowed = {root / "communication" / "contacts.py", root / "communication" / "connectors.py",
               root / "db" / "models" / "__init__.py", root / "db" / "models" / "communication_delivery.py"}
    sensitive = {"ContactResolver", "ContactPoint", "ResolvedContact"}

    def imports_contacts(path: Path) -> bool:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and ("communication.contacts" in (node.module or "")
                                                    or sensitive & {a.name for a in node.names}):
                return True
            if isinstance(node, ast.Import) and any("communication.contacts" in a.name for a in node.names):
                return True
        return False

    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if p not in allowed and "voice" not in p.parts and imports_contacts(p)]
    assert offenders == []


def test_no_contact_details_in_jobs_events_audit_or_missions(client, small_class, runtime, clock, session_factory,
                                                             orgs) -> None:
    with tenant(session_factory, orgs["a"]) as s:
        s.add(ContactPoint(student_id=small_class["students"]["GRD-001"], kind=ContactKind.PHONE, address=PHONE,
                           verified=True))
        s.commit()
    job = ready_job(client.app, client, clock, small_class, session_factory, orgs)
    comm(client.app, clock)
    advance(client.app, orgs["a"], job.agent_mission_id)

    def dump(model, *columns):
        return json.dumps([{c: str(getattr(r, c)) for c in columns} for r in rows(session_factory, model)])

    text = dump(CommunicationJob, *CommunicationJob.__table__.columns.keys())
    text += dump(CommunicationAttempt, *CommunicationAttempt.__table__.columns.keys())
    text += json.dumps([e.payload for e in rows(session_factory, DomainEvent)])
    text += json.dumps([e.event_metadata for e in rows(session_factory, OperationAuditEvent)])
    text += json.dumps([m.context for m in rows(session_factory, AgentMission, AgentMission.agent_key == AGENT)])
    text += dump(AgentStep, "input_summary", "output_summary")
    assert "98765" not in text and "meridian" not in text and "Student GRD" not in text
    for model in (CommunicationJob, CommunicationAttempt):
        assert not {"phone", "email", "address", "transcript", "audio"} & set(model.__table__.columns.keys())


def test_message_templates_are_deterministic_and_safe() -> None:
    facts = SourceFacts("assignment", "Lab 3", "GRD301", T0)
    assert compose_message("submission_reminder", facts) == compose_message("submission_reminder", facts)
    title, body = compose_message("deadline_warning", facts)
    assert title == "Deadline soon: Lab 3" and "GRD301" in body and "07 Oct 2026" in body
    assert compose_message("unknown_purpose", facts)[0] == "Message from your institution"


# --- Communication policy boundaries -----------------------------------------------------------------------------------


def _check(**overrides):
    args = dict(now=T0, source_open=True, authorization="standing_policy", channel="voice", attempts_so_far=0,
                not_before=None, consent=cp.Consent(voice=True), policy=COMM_POLICY)
    return cp.check_delivery(**{**args, **overrides})


def test_delivery_gate_order_and_boundaries() -> None:
    assert _check().allowed
    assert _check(source_open=False, authorization="approval_required").reason == "SOURCE_RESOLVED"
    assert _check(authorization="approval_required").reason == "APPROVAL_PENDING"
    assert _check(authorization="rejected").reason == "APPROVAL_REJECTED"
    assert _check(attempts_so_far=1).allowed and _check(attempts_so_far=2).reason == "MAX_ATTEMPTS_REACHED"
    assert _check(not_before=T0).allowed  # exactly at not_before
    late = _check(not_before=T0 + timedelta(seconds=1))
    assert (late.reason, late.defer_until) == ("NOT_YET", T0 + timedelta(seconds=1))
    quiet = cp.CommunicationPolicy(contact=FollowupPolicy(quiet_start=time(3, 0), quiet_end=time(5, 0)))
    blocked = _check(policy=quiet)  # T0 is 04:00 UTC
    assert (blocked.reason, blocked.defer_until) == ("QUIET_HOURS", T0 + timedelta(hours=1))
    assert _check(policy=quiet, channel="in_app").allowed  # a silent in-app notice is exempt
    personal = cp.Consent(voice=True, quiet_start=time(4, 0), quiet_end=time(4, 30))
    assert _check(consent=personal).reason == "QUIET_HOURS"  # the student's own quiet hours override


def test_retry_backoff_and_channel_rules() -> None:
    policy = cp.CommunicationPolicy(max_attempts=3, backoff=timedelta(minutes=30), max_backoff=timedelta(minutes=45))
    first = cp.after_failed_attempt(now=T0, attempt_number=1, error_code="NO_ANSWER", policy=policy)
    second = cp.after_failed_attempt(now=T0, attempt_number=2, error_code="BUSY", policy=policy)
    assert (first.retry, first.next_attempt_at) == (True, T0 + timedelta(minutes=30))
    assert second.next_attempt_at == T0 + timedelta(minutes=45)  # doubled, then capped
    assert cp.after_failed_attempt(now=T0, attempt_number=3, error_code="BUSY", policy=policy).reason == "MAX_ATTEMPTS_REACHED"
    assert cp.after_failed_attempt(now=T0, attempt_number=1, error_code="POSTCONDITION_FAILED",
                                   policy=policy).reason == "NON_RETRYABLE_ERROR"
    consent = cp.Consent(voice=True, preferred="voice")
    assert cp.permitted_channels("assignment", "submission_reminder", consent, ["voice"]) == ["voice", "in_app"]
    assert cp.permitted_channels("nexus", "anything", consent, ["voice", "email"]) == ["in_app", "email"]
    kwargs = dict(source_type="assignment", purpose="submission_reminder", consent=consent, available=["voice"],
                  previous_channel="voice", previous_failed=True)
    assert cp.check_channel("in_app", policy=policy, **kwargs) == "CHANNEL_SWITCH_NOT_ALLOWED"
    assert cp.check_channel("in_app", policy=cp.CommunicationPolicy(allow_channel_fallback=True), **kwargs) is None
    assert cp.check_channel("email", policy=policy, **{**kwargs, "previous_channel": None}) == "PROVIDER_UNAVAILABLE"
    assert cp.authorization_for("exam", "exam_reminder") == "standing_policy"
    assert cp.authorization_for("nexus", "exam_reminder") == "approval_required"
    assert cp.map_exotel_status("no-answer").error_code == "NO_ANSWER" and not cp.map_exotel_status("weird").terminal
