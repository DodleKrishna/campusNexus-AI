"""Pure presentation logic for the Streamlit UI (Phase 14).

Turns the API's real, persisted mission data into short human-readable labels,
stages and summaries. Nothing here computes a rule, invents a status or calls
the backend: every value is read from a field the API already returned, and
anything that is not there is reported as not there. No Streamlit calls, so it
is unit-tested directly (tests/test_phase14_presenters.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

# The seeded campus is in India; times are stored in UTC and shown in IST.
CAMPUS_TZ = timezone(timedelta(hours=5, minutes=30))
CAMPUS_TZ_LABEL = "IST"

_RUN_STATUS = {"success": "VERIFIED", "partial": "NEEDS REVIEW", "failed": "FAILED"}
_TASK_STATUS = {
    "pending": "PENDING",
    "in_progress": "RUNNING",
    "blocked": "NEEDS REVIEW",
    "skipped": "SKIPPED",
    "failed": "FAILED",
    "completed": "VERIFIED",
}
_ACADEMIC_LABELS = {
    "timetable": "Academic Schedule",
    "exam_schedule": "Academic Exams",
    "attendance_status": "Attendance Check",
    "attendance_recovery": "Attendance Recovery Plan",
    "exam_eligibility": "Exam Eligibility",
    "policy_question": "Academic Policy",
}
_AGENT_LABELS = {
    "academic_agent": "Academic Check",
    "career_agent": "Career Analysis",
    "events_opportunity_agent": "Event Discovery",
    "campus_services_agent": "Grievance & SLA Check",
    "action_agent": "Action",
}
_WORKING_MESSAGES = {
    "academic_agent": "Academic Agent checking academic records…",
    "career_agent": "Career Agent evaluating opportunities…",
    "events_opportunity_agent": "Events Agent matching recommendations…",
    "campus_services_agent": "Campus Services Agent checking cases and SLAs…",
    "action_agent": "Action Agent preparing the action and running the pre-check…",
}
_TOOL_RESULTS = {
    "register_event": ("Registration successful", "Registration verified in the database after execution"),
    "create_calendar_event": ("Calendar entry created", "Calendar entry verified in the database after execution"),
    "create_campus_case": ("Complaint filed", "Case verified in the database after execution"),
}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fmt_when(start: Optional[str], end: Optional[str] = None) -> str:
    """'Sat 03 Oct 2026, 15:00–18:00 IST' (campus time); '' when unknown."""
    begin = _parse(start)
    if begin is None:
        return ""
    begin = begin.astimezone(CAMPUS_TZ)
    text = begin.strftime("%a %d %b %Y, %H:%M")
    finish = _parse(end)
    if finish is not None:
        finish = finish.astimezone(CAMPUS_TZ)
        text += "–" + (finish.strftime("%H:%M") if finish.date() == begin.date() else finish.strftime("%d %b %H:%M"))
    return f"{text} {CAMPUS_TZ_LABEL}"


def plural(count: int, word: str, plural_word: Optional[str] = None) -> str:
    return f"{count} {word if count == 1 else (plural_word or word + 's')}"


# ---------------------------------------------------------------------------
# Tasks: labels, verification summaries, evidence sources
# ---------------------------------------------------------------------------


def task_label(agent: str, facts: Optional[Dict] = None) -> str:
    """A readable stage name from the agent and its classified intent."""
    facts = facts or {}
    if agent == "academic_agent":
        return _ACADEMIC_LABELS.get(str(facts.get("intent")), _AGENT_LABELS[agent])
    if agent == "events_opportunity_agent" and "matched_against_skill_gaps" in facts:
        return "Event Matching"
    return _AGENT_LABELS.get(agent, agent)


def latest_runs(mission: Dict) -> Dict[str, Dict]:
    """The most recent agent run per task id."""
    latest: Dict[str, Dict] = {}
    for run in mission.get("agent_results") or []:
        latest[run["task_id"]] = run
    return latest


def _mission_schedule_counts(mission: Dict) -> tuple:
    """(timetable slots, exams) from this mission's verified Academic results."""
    slots = exams = None
    for run in latest_runs(mission).values():
        if run["agent"] != "academic_agent" or run["status"] != "success":
            continue
        facts = run.get("facts") or {}
        if isinstance(facts.get("timetable"), list) and slots is None:
            slots = len(facts["timetable"])
        if isinstance(facts.get("exams"), list) and exams is None:
            exams = len(facts["exams"])
    return slots, exams


def evidence_sources(evidence: Sequence[Dict]) -> List[str]:
    """Unique 'Title (version)' labels, in retrieval order."""
    seen: List[str] = []
    for item in evidence or []:
        label = item.get("title") or item.get("document_id") or "Policy document"
        if item.get("policy_version"):
            label += f" ({item['policy_version']})"
        if label not in seen:
            seen.append(label)
    return seen


def _policy_title(evidence: Sequence[Dict], document_id: Optional[str]) -> Optional[str]:
    for item in evidence or []:
        if document_id and item.get("document_id") == document_id:
            return f"{item.get('title')} ({item.get('policy_version')})" if item.get("policy_version") else item.get("title")
    return None


def verification_summary(run: Dict, mission: Optional[Dict] = None) -> Optional[str]:
    """One plain sentence on what the deterministic check actually used, from
    the run's own facts. None when the facts do not say."""
    facts = run.get("facts") or {}
    agent = run.get("agent")
    if run.get("status") == "failed" and agent != "action_agent":
        return None
    if agent == "academic_agent":
        attendance, threshold = facts.get("attendance"), facts.get("threshold") or {}
        if isinstance(attendance, dict) and attendance.get("classes_conducted") is not None:
            policy = _policy_title(run.get("evidence") or [], threshold.get("document_id")) or threshold.get("document_id")
            text = (
                f"Attendance calculated from current student records "
                f"({attendance.get('classes_attended')}/{attendance.get('classes_conducted')} classes)"
            )
            if threshold.get("required_percentage") is not None:
                text += f"; required {threshold['required_percentage']}%" + (f" from {policy}" if policy else "")
            return text + "."
        if isinstance(facts.get("timetable"), list):
            return f"Read from current student records: {plural(len(facts['timetable']), 'weekly class slot')}."
        if isinstance(facts.get("exams"), list):
            return f"Read from current student records: {plural(len(facts['exams']), 'exam')}."
        return None
    if agent == "career_agent" and isinstance(facts.get("eligibilities"), list):
        eligible = sum(1 for e in facts["eligibilities"] if e.get("status") == "eligible")
        text = (
            f"Eligibility calculated from current student records: {eligible} of "
            f"{plural(len(facts['eligibilities']), 'opportunity', 'opportunities')} eligible"
        )
        gaps = facts.get("skill_gaps")
        return text + (f"; {plural(len(gaps), 'skill gap')} identified." if isinstance(gaps, list) else ".")
    if agent == "events_opportunity_agent":
        assessments = facts.get("assessments") or []
        if not assessments:
            return None
        if all(a.get("conflict_check_performed") for a in assessments):
            slots, exams = _mission_schedule_counts(mission or {})
            against = (
                f" against {plural(slots, 'timetable slot')} and {plural(exams, 'exam')}"
                if slots is not None and exams is not None else ""
            )
            text = f"{plural(len(assessments), 'event')} checked for schedule conflicts{against}"
        else:
            text = f"{plural(len(assessments), 'event')} found; schedule conflicts were not checked in this step"
        if "matched_against_skill_gaps" in facts:
            text += "; matched to your skill gaps"
        return text + "."
    if agent == "campus_services_agent" and isinstance(facts.get("case_assessments"), list):
        cases = facts["case_assessments"]
        breached = sum(1 for c in cases if c.get("response_breached") or c.get("resolution_breached"))
        return f"SLA status computed from {plural(len(cases), 'case record')}: {breached} breached."
    if agent == "action_agent":
        if facts.get("postcondition_verified") is True:
            return _TOOL_RESULTS.get(str(facts.get("tool_name")), ("", "Result verified in the database after execution"))[1] + "."
        if facts.get("precheck_status"):
            schedule = ((facts.get("proposal") or {}).get("supporting_facts") or {}).get("schedule_check") or {}
            text = f"Pre-check {str(facts['precheck_status']).upper()}"
            if schedule.get("performed"):
                text += (
                    f": checked against {plural(schedule.get('timetable_entries_checked') or 0, 'timetable slot')} "
                    f"and {plural(schedule.get('exam_entries_checked') or 0, 'exam')}"
                )
            return text + "."
    return None


# ---------------------------------------------------------------------------
# Mission progress + state
# ---------------------------------------------------------------------------


@dataclass
class Stage:
    label: str
    status: str  # VERIFIED / RUNNING / PENDING / NEEDS REVIEW / FAILED / SKIPPED / WAITING / COMPLETED / APPROVED / ...
    detail: str = ""


def _task_stage_status(task: Dict, run: Optional[Dict]) -> str:
    if task["status"] in ("pending", "in_progress", "skipped") or run is None:
        return _TASK_STATUS.get(task["status"], task["status"].upper())
    return _RUN_STATUS.get(run["status"], run["status"].upper())


def _action_stages(mission: Dict, action_runs: List[Dict]) -> List[Stage]:
    proposal = next((r for r in reversed(action_runs) if r["facts"].get("precheck_status")), None)
    executed = next((r for r in reversed(action_runs) if "postcondition_verified" in r["facts"]), None)
    latest = action_runs[-1] if action_runs else None
    stages: List[Stage] = []

    if proposal is not None:
        stages.append(Stage("Action Pre-check", str(proposal["facts"]["precheck_status"]).upper().replace("_", " ")))
    elif latest is not None and latest["status"] == "failed":
        stages.append(Stage("Action Pre-check", "FAILED"))
    else:
        stages.append(Stage("Action Pre-check", "PENDING"))

    if mission.get("pending_approvals"):
        approval = "WAITING"
    elif executed is not None:
        approval = "APPROVED"
    elif latest is not None and latest["facts"].get("rejected"):
        approval = "REJECTED"
    elif mission.get("stale_approvals") and latest is not None and latest["status"] == "failed":
        approval = "EXPIRED"
    elif proposal is None:
        approval = "NOT REQUESTED"
    else:
        approval = "PENDING"
    stages.append(Stage("Approval", approval))

    if executed is not None:
        stages.append(Stage("Execution", "VERIFIED" if executed["facts"]["postcondition_verified"] else "FAILED"))
    elif latest is not None and latest["status"] == "failed" and latest["facts"].get("execution_recheck_status"):
        stages.append(Stage("Execution", "BLOCKED"))
    else:
        stages.append(Stage("Execution", "NOT STARTED"))
    return stages


def progress_stages(mission: Dict, candidates: Optional[Dict] = None) -> List[Stage]:
    """The mission as a short list of stages with real statuses.

    One stage per read-only task, then -- for a mission waiting on or holding
    a student selection -- the schedule check behind the candidates and the
    selection itself, then pre-check / approval / execution for an action.
    """
    runs = latest_runs(mission)
    stages: List[Stage] = []
    action_tasks = []
    plan = mission.get("plan") or []
    labels = {t["task_id"]: task_label(t["agent"], (runs.get(t["task_id"]) or {}).get("facts")) for t in plan}
    repeated = {label for label in labels.values() if list(labels.values()).count(label) > 1}
    for number, task in enumerate(plan, start=1):
        if task["agent"] == "action_agent":
            action_tasks.append(task)
            continue
        label = labels[task["task_id"]]
        if label in repeated:
            label += f" (T{number})"
        stages.append(Stage(label, _task_stage_status(task, runs.get(task["task_id"]))))

    selecting = mission.get("user_selection_required") or mission.get("selected_target")
    if selecting:
        if candidates is not None and candidates.get("candidates"):
            checked = any(c["assessment"].get("schedule_checked") for c in candidates["candidates"])
            stages.append(Stage("Schedule Check", "VERIFIED" if checked else "NOT CHECKED"))
        stages.append(Stage("Student Selection", "COMPLETED" if mission.get("selected_target") else "WAITING"))

    if action_tasks:
        action_ids = {t["task_id"] for t in action_tasks}
        action_runs = [r for r in mission.get("agent_results") or [] if r["task_id"] in action_ids]
        if any(t["status"] == "skipped" for t in action_tasks) and not action_runs:
            stages.append(Stage("Action", "SKIPPED"))
        else:
            stages.extend(_action_stages(mission, action_runs))
    return stages


@dataclass
class MissionState:
    key: str  # completed / user_selection / waiting_approval / needs_review / provider_unavailable / failed / running
    label: str
    message: str


def mission_state(mission: Dict) -> MissionState:
    """The one state a student should see first, from structured fields only."""
    status = mission.get("status")
    if mission.get("provider_unavailable"):
        if not mission.get("plan"):
            return MissionState(
                "provider_unavailable", "PROVIDER UNAVAILABLE",
                "Live AI is temporarily unavailable, so this goal could not be planned. Nothing was executed; run "
                "the goal again once the provider is available.",
            )
        return MissionState(
            "provider_unavailable", "PROVIDER UNAVAILABLE",
            "Live AI is temporarily unavailable. Your mission state has been preserved; resume it once the "
            "provider is available again.",
        )
    if mission.get("user_selection_required"):
        return MissionState(
            "user_selection", "USER SELECTION REQUIRED",
            "CampusNexus found suitable options. Choose the event you want to register for.",
        )
    if status == "needs_approval":
        if mission.get("pending_approvals"):
            return MissionState(
                "waiting_approval", "WAITING FOR APPROVAL",
                "An administrator must approve this action in the Action Center. Nothing runs until then; "
                "afterwards, click Resume after approval.",
            )
        return MissionState(
            "needs_review", "NEEDS REVIEW",
            "Some results could not be fully verified and need review before the mission can continue.",
        )
    if status == "failed":
        return MissionState("failed", "FAILED", "This mission did not complete. The results below explain why.")
    if status == "completed":
        return MissionState("completed", "COMPLETED", "Mission completed. Every result below was verified.")
    return MissionState("running", "IN PROGRESS", "This mission is still running.")


def working_message(summary: Optional[Dict]) -> str:
    """What is happening right now, from a MissionSummaryView (no invented %)."""
    if not summary:
        return "Creating mission plan…"
    running = summary.get("running_step")
    if running:
        return _WORKING_MESSAGES.get(running["agent"], "Agents working…")
    if summary.get("steps_total", 0) == 0:
        return "Creating mission plan…"
    if summary.get("steps_done", 0) >= summary.get("steps_total", 0):
        return "Verifier checking results…"
    return "Scheduling the next tasks…"


# ---------------------------------------------------------------------------
# Action result + candidates
# ---------------------------------------------------------------------------


@dataclass
class ActionResult:
    verified: bool
    headline: str
    title: str
    target: str
    rows: List[tuple]
    note: str = ""


def _action_target(mission: Dict, action_runs: List[Dict]) -> str:
    if mission.get("selected_target"):
        return mission["selected_target"]["title"]
    for run in reversed(action_runs):
        value = (run["facts"].get("target_provenance") or {}).get("target_value")
        if value:
            return value
    return ""


def action_result(mission: Dict) -> Optional[ActionResult]:
    """The outcome of the mission's action once it is past the approval pause."""
    runs = [r for r in mission.get("agent_results") or [] if r["agent"] == "action_agent"]
    if not runs or runs[-1]["facts"].get("awaiting_approval"):
        return None
    latest = runs[-1]
    facts = latest["facts"]
    tool = str(facts.get("tool_name") or "")
    target = _action_target(mission, runs)
    if "postcondition_verified" in facts:
        tool_result = facts.get("tool_result") or {}
        verified = bool(facts["postcondition_verified"]) and latest["status"] == "success"
        return ActionResult(
            verified=verified,
            headline="ACTION VERIFIED" if verified else "ACTION NOT VERIFIED",
            title=_TOOL_RESULTS.get(tool, ("Action executed", ""))[0] if verified else "Postcondition check did not pass",
            target=target,
            rows=[
                ("Pre-execution check", str(facts.get("execution_recheck_status") or "not re-run").upper().replace("_", " ")),
                ("Database write", "ALREADY DONE" if facts.get("already_executed") or tool_result.get("already_existed") else "SUCCESS"),
                ("Postcondition check", "VERIFIED" if facts["postcondition_verified"] else "FAILED"),
            ],
        )
    reason = "; ".join(latest.get("errors") or []) or (latest.get("response_text") or "")
    return ActionResult(
        verified=False, headline="ACTION BLOCKED", title="Nothing was written", target=target,
        rows=[("Database write", "NOT PERFORMED")], note=reason,
    )


CANDIDATE_STATUS_TEXT = {
    "eligible": "No class/exam conflict · Registration open",
    "conflict": "Schedule conflict",
    "full": "Event is full",
    "deadline_passed": "Registration deadline passed",
    "already_registered": "You are already registered",
    "unavailable": "Registration not open",
    "needs_review": "Could not be fully checked",
}


def candidate_reason(candidate: Dict) -> str:
    """The shortest accurate reason an unsafe candidate cannot be selected."""
    assessment = candidate.get("assessment") or {}
    clashes = [f"{c['course_code']} class" for c in assessment.get("timetable_conflicts") or []]
    clashes += [f"{c['course_code']} {c['exam_type']} exam" for c in assessment.get("exam_conflicts") or []]
    if clashes:
        return "Clashes with " + ", ".join(clashes)
    reasons = assessment.get("reasons") or []
    return reasons[0] if reasons else ""


def split_candidates(candidates: Sequence[Dict]) -> tuple:
    """(selectable, blocked-but-relevant, not-open) -- the last group is shown collapsed."""
    selectable = [c for c in candidates if c.get("selectable")]
    not_open = [c for c in candidates if not c.get("selectable") and c["assessment"]["status"] == "unavailable"]
    blocked = [c for c in candidates if not c.get("selectable") and c["assessment"]["status"] != "unavailable"]
    key = lambda c: c.get("start_at") or ""  # noqa: E731
    return sorted(selectable, key=key), sorted(blocked, key=key), sorted(not_open, key=key)
