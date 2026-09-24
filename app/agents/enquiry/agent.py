"""The Enquiry Agent (Phase 15) -- answers general campus questions by consulting specialists.

    question -> plan (LLM, structured ``EnquiryPlan``) -> consult specialists
    -> keep only VERIFIED results -> deterministic synthesis

Read-only by construction: it can only reach specialists through the
``consult`` callable it is given (``SpecialistGateway.consult``), which
refuses anything but the four read-only agents. It never touches the Action
Agent, the Tool Gateway or the Approval Gate, so an action request is only
ever answered with where that action belongs.

The synthesis is plain code over verified facts and the specialists' own
verified answers. The LLM chooses whom to ask; it never writes the answer, so
the answer cannot contain a fact no specialist returned.

Phase 16: live class questions ("has my class started?", "was I marked
present?", "what class is next?") are answered from the real attendance
session through the ``live_class`` callable (``class_schedule.get_current_class``),
whose deterministic message is used verbatim. Without that callable the agent
still says honestly that live status is unavailable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from app.llm.base import LLMProvider
from app.schemas.agent_chat import (
    DISPLAY_NAMES,
    ActionHint,
    AgentQueryResponse,
    EnquiryPlan,
    SpecialistAnswer,
)
from app.schemas.enums import VerificationStatus
from app.schemas.faculty import LiveClassStatus

CAMPUS_TZ = timezone(timedelta(hours=5, minutes=30))
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ACTION_DESTINATIONS = {
    "events": "Event registration goes through CampusNexus's selection-and-approval workflow: pick an event from "
              "the candidates, then an approver signs off before anything is registered.",
    "placements": "Applications are not submitted from chat. Open the Placement Agent to review eligibility first.",
    "complaints": "Filing a complaint needs the approval-gated complaint workflow, not chat.",
    "academic": "Academic records cannot be changed from chat.",
}
_LIVE_CLASS_UNAVAILABLE = "live class-start status is not currently available."

Consult = Callable[[str, str], SpecialistAnswer]
LiveClass = Callable[[datetime], LiveClassStatus]


def _local(now: datetime) -> datetime:
    return now.astimezone(CAMPUS_TZ)


def _today_classes(answer: SpecialistAnswer, now: datetime) -> Optional[List[dict]]:
    timetable = answer.facts.get("timetable")
    if not isinstance(timetable, list):
        return None
    weekday = _local(now).weekday()
    return sorted((s for s in timetable if s.get("weekday") == weekday), key=lambda s: s.get("start_time", ""))


def _live_class_line(answer: SpecialistAnswer, now: datetime) -> str:
    classes = _today_classes(answer, now) or []
    clock = _local(now).strftime("%H:%M")
    current = next((s for s in classes if s["start_time"] <= clock < s["end_time"]), None)
    if current is not None:
        return (
            f"Your timetable shows {current['course_title']} scheduled now ({current['start_time']}–{current['end_time']}, "
            f"{current['location']}), but {_LIVE_CLASS_UNAVAILABLE}"
        )
    upcoming = next((s for s in classes if s["start_time"] > clock), None)
    if upcoming is not None:
        return (
            f"No class is scheduled right now. Next on your timetable: {upcoming['course_title']} at "
            f"{upcoming['start_time']} ({upcoming['location']}). Note that {_LIVE_CLASS_UNAVAILABLE}"
        )
    return f"No more classes are scheduled on your timetable today. Note that {_LIVE_CLASS_UNAVAILABLE}"


def _headline(answer: SpecialistAnswer, now: datetime) -> Optional[str]:
    """One factual line from a verified specialist's structured facts, or None."""
    facts = answer.facts
    local = _local(now)
    if answer.agent_key == "academic":
        classes = _today_classes(answer, now)
        if classes is not None:
            if not classes:
                return f"Classes today ({_WEEKDAYS[local.weekday()]}): none on your timetable."
            listed = "; ".join(f"{s['start_time']}–{s['end_time']} {s['course_title']} ({s['location']})" for s in classes)
            return f"Classes today ({_WEEKDAYS[local.weekday()]}): {listed}."
        exams = facts.get("exams")
        if isinstance(exams, list):
            upcoming = sorted(
                (e for e in exams if datetime.fromisoformat(e["scheduled_start"]) >= now),
                key=lambda e: e["scheduled_start"],
            )
            if not upcoming:
                return "Exams: none upcoming."
            first = upcoming[0]
            when = _local(datetime.fromisoformat(first["scheduled_start"])).strftime("%a %d %b, %H:%M")
            return f"Next exam: {first['course_title']} {first['exam_type']} on {when} IST ({first['location']})."
        return None
    if answer.agent_key == "events":
        assessments = facts.get("assessments")
        if not isinstance(assessments, list):
            return None
        registered = [a["event"]["title"] for a in assessments if a.get("already_registered")]
        clear = [
            a for a in assessments
            if a.get("availability") == "available" and not a.get("already_registered")
            and a.get("conflict_check_performed") and not a.get("timetable_conflicts") and not a.get("exam_conflicts")
        ]
        line = f"Events: {len(clear)} open for registration with no clash with your classes or exams"
        return line + (f"; you are registered for {', '.join(registered)}." if registered else ".")
    if answer.agent_key == "placements":
        eligibilities = facts.get("eligibilities")
        if not isinstance(eligibilities, list):
            return None
        eligible = [e for e in eligibilities if e.get("status") == "eligible"]
        applied = [a for a in facts.get("applications") or []]
        return f"Placements: eligible for {len(eligible)} open opportunit{'y' if len(eligible) == 1 else 'ies'}; {len(applied)} application(s) in progress."
    if answer.agent_key == "complaints":
        cases = facts.get("case_assessments")
        if not isinstance(cases, list):
            return None
        overdue = [c["case"]["case_code"] for c in cases if c.get("response_breached") or c.get("resolution_breached")]
        return f"Complaints: {len(cases)} case(s); " + (f"past SLA: {', '.join(overdue)}." if overdue else "none past SLA.")
    return None


class EnquiryAgent:
    def __init__(self, *, llm_provider: LLMProvider, consult: Consult, live_class: Optional[LiveClass] = None) -> None:
        self._llm = llm_provider
        self._consult = consult
        self._live_class = live_class

    def handle(self, message: str, *, now: Optional[datetime] = None) -> AgentQueryResponse:
        now = now or datetime.now(timezone.utc)
        plan: EnquiryPlan = self._llm.plan_enquiry(message)
        answers = [self._consult(c.agent, c.objective) for c in plan.consultations]
        verified = [a for a in answers if a.verification_status == VerificationStatus.VERIFIED.value]

        lines: List[str] = []
        facts: Dict[str, object] = {"plan": plan.model_dump(mode="json")}
        if plan.asks_live_class_status and self._live_class is not None:
            live = self._live_class(now)
            lines.append(live.message)
            upcoming = live.next_class
            if upcoming is not None and live.state in ("live", "scheduled"):
                lines.append(f"Next class today: {upcoming.course_title} at {upcoming.start_local} ({upcoming.room}).")
            facts["live_class"] = live.model_dump(mode="json")
        elif plan.asks_live_class_status:
            academic = next((a for a in verified if a.agent_key == "academic" and _today_classes(a, now) is not None), None)
            lines.append(_live_class_line(academic, now) if academic else f"Your timetable could not be verified, and {_LIVE_CLASS_UNAVAILABLE}")
        seen: set = set()
        for answer in verified:
            if plan.asks_live_class_status and answer.agent_key == "academic" and _today_classes(answer, now) is not None:
                continue
            # A one-line summary from structured facts when the facts support one;
            # otherwise the specialist's own verified answer.
            text = _headline(answer, now) or answer.answer.strip()
            if text and text not in seen:
                seen.add(text)
                lines.append(text)

        notices = [
            f"{DISPLAY_NAMES[a.agent_key]}'s result could not be verified, so it was left out."
            for a in answers if a.verification_status != VerificationStatus.VERIFIED.value
        ]
        action_hint = None
        if plan.action_requested:
            key = plan.action_agent or "events"
            action_hint = ActionHint(
                agent_key=key,
                message="I can look things up but I can't make changes. " + _ACTION_DESTINATIONS.get(key, ""),
            )
        if plan.out_of_scope:
            lines.append(plan.out_of_scope)
        if not lines:
            lines.append(action_hint.message if action_hint else "None of the campus agents could verify an answer to that.")

        if not answers:
            status = VerificationStatus.VERIFIED.value if "live_class" in facts else "not_applicable"
        elif len(verified) == len(answers):
            status = VerificationStatus.VERIFIED.value
        elif verified:
            status = VerificationStatus.NEEDS_REVIEW.value
        else:
            status = VerificationStatus.FAILED.value
        return AgentQueryResponse(
            agent_key="enquiry", display_name=DISPLAY_NAMES["enquiry"], verification_status=status,
            answer="\n".join(lines), facts=facts,
            evidence=[e for a in verified for e in a.evidence], consulted=answers, notices=notices,
            action_hint=action_hint, live_ai=bool(getattr(self._llm, "is_live", False)),
        )
