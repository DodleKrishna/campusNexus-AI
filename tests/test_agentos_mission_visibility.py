"""CAMPUS AI: GET /agentos/autonomous-missions -- Guardian / Communication mission visibility.

Tenant, role and subject scoped: a student sees only missions that concern them and only their own progress; a faculty
member sees the classes they teach; the HOD their department; an admin the organization. Structured status only.
"""
from __future__ import annotations

import json

from app.db.models import AgentMission
from tests.test_agentos_assignment_guardian import (  # noqa: F401 -- fixtures
    auth, clock, email, guardian, published, run_worker, small_class, submit,
)
from tests.test_agentos_exam_attendance_guardians import guardians, scheduled_exam  # noqa: F401 -- fixtures
from tests.test_phase22b_tenant_isolation import (  # noqa: F401 -- fixtures
    A_ADMIN, A_FACULTY, A_STUDENT, B_ADMIN, B_FACULTY, app, bearer, client, login, orgs,
)

URL = "/agentos/autonomous-missions"
A_HOD = "hod@campusnexus.local"
OTHER_FACULTY = "manoj.pillai@campusnexus.local"  # seeded; does not teach the class
FORBIDDEN_KEYS = {"goal", "context", "input_summary", "output_summary", "assistant_message", "phone", "email",
                  "address", "transcript", "audio", "prompt", "reasoning", "content_text"}


def visible(client, who: str, **params) -> dict:
    response = client.get(URL, headers=auth(client, who), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def ids(page: dict) -> set:
    return {item["mission_id"] for item in page["items"]}


def keys(value) -> set:
    if isinstance(value, dict):
        return set(value) | set().union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value)) if value else set()
    return set()


def test_assignment_guardian_visibility_by_role(client, small_class, guardian, clock, session_factory, orgs) -> None:
    assignment = published(client, small_class)
    mission_id = assignment["guardian_mission_id"]
    run_worker(client.app)
    assert submit(client, "GRD-001", assignment["assignment"]["id"], content_text="my private answer").status_code in (200, 201)

    staff = visible(client, A_FACULTY)
    item = next(i for i in staff["items"] if i["mission_id"] == mission_id)
    assert (item["agent_key"], item["kind"], item["subject"]["type"]) == ("assignment_guardian", "guardian", "assignment")
    assert item["progress"] == {"scope": "class", "total": 3, "resolved": 1, "pending": 2, "own_status": None}
    assert item["activity"] and all(set(a) == {"step_number", "action_type", "tool_name", "status", "created_at"}
                                    for a in item["activity"])

    own = visible(client, email("GRD-001"))["items"]
    assert [i["mission_id"] for i in own] == [mission_id]
    assert own[0]["progress"] == {"scope": "self", "total": None, "resolved": None, "pending": None,
                                  "own_status": "submitted"}
    assert own[0]["activity"] == [] and own[0]["result_code"] is None  # no class-level facts for a student
    assert visible(client, email("GRD-002"))["items"][0]["progress"]["own_status"] == "pending"

    assert mission_id in ids(visible(client, A_HOD))  # head of the class's department
    assert mission_id in ids(visible(client, A_ADMIN))
    for outsider in (A_STUDENT, OTHER_FACULTY, B_FACULTY, B_ADMIN):  # not targeted / not teaching / other tenant
        assert mission_id not in ids(visible(client, outsider))


def test_the_view_holds_no_free_text_contacts_or_reasoning(client, small_class, guardian, clock) -> None:
    assignment = published(client, small_class)
    run_worker(client.app)
    submit(client, "GRD-001", assignment["assignment"]["id"], content_text="my private answer")
    for who in (A_FACULTY, A_ADMIN, email("GRD-001")):
        page = visible(client, who)
        assert page["items"]
        assert not keys(page) & FORBIDDEN_KEYS
        text = json.dumps(page)
        assert "my private answer" not in text and "@" not in text and "Ensure every targeted" not in text


def test_exam_guardian_is_visible_to_targets_and_teaching_staff(client, small_class, guardians, clock, session_factory,
                                                                 orgs) -> None:
    exam = scheduled_exam(client, small_class)
    mission_id = exam["guardian_mission_id"]
    item = next(i for i in visible(client, A_FACULTY)["items"] if i["mission_id"] == mission_id)
    assert item["subject"]["type"] == "exam" and item["progress"]["total"] == 3
    own = next(i for i in visible(client, email("GRD-003"))["items"] if i["mission_id"] == mission_id)
    assert own["progress"]["own_status"] == "pending" and own["activity"] == []
    assert mission_id not in ids(visible(client, A_STUDENT))


def test_nexus_missions_are_not_listed_and_pagination_is_bounded(client, small_class, guardian, clock,
                                                                 session_factory) -> None:
    for _ in range(3):
        published(client, small_class)
    client.post("/agentos/assistant/message", headers=auth(client, A_FACULTY), json={"message": "Who am I?"})
    first = visible(client, A_ADMIN, limit=2)
    assert len(first["items"]) == 2 and first["next_before_id"]
    rest = visible(client, A_ADMIN, limit=2, before_id=first["next_before_id"])
    assert not ids(first) & ids(rest)
    with session_factory() as s:
        agent_keys = {s.get(AgentMission, i).agent_key for i in ids(first) | ids(rest)}
    assert agent_keys == {"assignment_guardian"}
    assert client.get(URL, headers=auth(client, A_ADMIN), params={"limit": 51}).status_code == 422
    assert client.get(URL, headers=auth(client, A_ADMIN), params={"before_id": 0}).status_code == 422


def test_requires_authentication_and_refuses_extra_scope_inputs(client, small_class, guardian, clock) -> None:
    assert client.get(URL).status_code == 401
    published(client, small_class)
    # A client-supplied organization/student is ignored: the scope comes from the token only.
    page = client.get(URL, headers=auth(client, A_STUDENT), params={"organization_id": 999, "student_id": 1}).json()
    assert page["items"] == []


def test_attendance_guardian_is_visible_only_to_the_absent_student_and_class_staff(client, small_class, guardians, clock,
                                                                                    session_factory, orgs) -> None:
    from tests.test_agentos_exam_attendance_guardians import absent_class, interventions
    absent_class(client, clock, session_factory, orgs, small_class)
    [case] = interventions(session_factory)
    own = [i for i in visible(client, email("GRD-003"))["items"] if i["agent_key"] == "attendance_guardian"]
    assert [i["mission_id"] for i in own] == [case.mission_id]
    assert own[0]["progress"]["own_status"] == "open" and own[0]["activity"] == []
    assert case.mission_id not in ids(visible(client, email("GRD-001")))  # a classmate never sees it
    assert case.mission_id in ids(visible(client, A_FACULTY)) and case.mission_id in ids(visible(client, A_HOD))
    assert case.mission_id not in ids(visible(client, OTHER_FACULTY))


def test_communication_missions_follow_their_recipient_and_source(client, small_class, clock, session_factory,
                                                                   orgs) -> None:
    from tests.test_agentos_communication import comm, jobs, requested, use_runtime
    use_runtime(client.app)
    requested(client, small_class, session_factory, orgs)  # one follow-up request for GRD-001
    comm(client.app, clock)
    (job,) = jobs(session_factory)
    item = next(i for i in visible(client, email("GRD-001"))["items"] if i["mission_id"] == job.agent_mission_id)
    assert (item["kind"], item["subject"]["type"], item["progress"]["scope"]) == ("communication", "communication", "self")
    assert "@" not in json.dumps(item)
    assert job.agent_mission_id not in ids(visible(client, email("GRD-002")))
    assert job.agent_mission_id in ids(visible(client, A_FACULTY))  # via its source Guardian's class
    assert job.agent_mission_id not in ids(visible(client, OTHER_FACULTY))
