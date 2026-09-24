"""Campus Dashboard section (Phase 8 §5; Phase 14 first-screen polish) -- real
database-backed information for the selected student, composed entirely from
GET /students/{id}/dashboard and GET /students/{id}/missions. No numbers are
computed or invented here beyond counting rows the API returned.
"""
from __future__ import annotations

import streamlit as st

from streamlit_app import presenters, theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError

_OPEN_CASE_STATUSES = ("open", "in_progress")
_AVAILABILITY = {
    "available": "registration open",
    "full": "full",
    "registration_closed": "registration closed",
    "not_open": "not open yet",
}


def _open_mission(mission_id: str) -> None:
    st.session_state["cn_current_mission_id"] = mission_id
    st.session_state["cn_page"] = "Mission Workspace"


def render(client: ApiClient, student_id: str) -> None:
    st.subheader("Campus Dashboard")
    try:
        data = client.get_dashboard(student_id)
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"Could not load the dashboard ({exc.status_code}): {exc.detail}")
        return
    try:
        missions = client.list_student_missions(student_id, limit=10)
    except (ApiUnavailableError, ApiError):
        missions = []

    student = data["student"]
    at_risk = [a for a in data["attendance"] if a["current_percentage"] is not None and not a["eligible_now"]]
    overdue = [g for g in data["grievances"] if g["response_breached"] or g["resolution_breached"]]
    open_cases = [g for g in data["grievances"] if g["status"] in _OPEN_CASE_STATUSES]
    eligible = [o for o in data["recommended_opportunities"] if o["status"] == "eligible"]
    next_exam = data["upcoming_exams"][0] if data["upcoming_exams"] else None

    cols = st.columns(5)
    with cols[0]:
        theme.render_metric_card(
            "Student", student["full_name"],
            f"{student['student_code']} · {student['department_code']} · Year {student['year']} · CGPA {student['cgpa']:.2f}",
        )
    with cols[1]:
        risk_note = ", ".join(f"{a['course_code']} {a['current_percentage']}%" for a in at_risk) or "all courses above requirement"
        theme.render_metric_card("Attendance risk", presenters.plural(len(at_risk), "course"), risk_note)
    with cols[2]:
        theme.render_metric_card(
            "Next exam", next_exam["course_code"] if next_exam else "None",
            presenters.fmt_when(next_exam["scheduled_start"]) if next_exam else "no upcoming exams",
        )
    with cols[3]:
        theme.render_metric_card("Open opportunities", str(len(eligible)), "internships you are eligible for")
    with cols[4]:
        theme.render_metric_card("Grievances", f"{len(open_cases)} open", f"{len(overdue)} past SLA")

    _render_active_missions(missions)

    st.markdown("#### Attendance Overview")
    if not data["attendance"]:
        st.caption("No attendance records found.")
    for item in data["attendance"]:
        with st.container(border=True):
            row = st.columns([3, 2, 3])
            row[0].markdown(f"**{item['course_title']}** ({item['course_code']})")
            row[1].write(f"{item['classes_attended']}/{item['classes_conducted']} classes")
            if item["current_percentage"] is not None:
                badge = theme.status_badge("verified" if item["eligible_now"] else "at risk")
                row[2].markdown(
                    f"{item['current_percentage']}% (required {item['required_percentage']}%) &nbsp; {badge}",
                    unsafe_allow_html=True,
                )
            else:
                row[2].markdown(f'<span class="cn-muted">policy threshold: {item["threshold_status"]}</span>', unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        st.markdown("#### Upcoming Examinations")
        if not data["upcoming_exams"]:
            st.caption("No upcoming exams.")
        for exam in data["upcoming_exams"]:
            st.markdown(
                f"- **{exam['course_title']}** ({exam['course_code']}) · {exam['exam_type']}  \n"
                f"  {presenters.fmt_when(exam['scheduled_start'])} · {exam['location']}"
            )

        st.markdown("#### Recommended Opportunities")
        if not data["recommended_opportunities"]:
            st.caption("No eligible opportunities right now.")
        for opp in data["recommended_opportunities"]:
            st.markdown(
                f"- **{opp['title']}** at {opp['company']} (min CGPA {opp['minimum_cgpa']}) &nbsp; {theme.status_badge(opp['status'])}",
                unsafe_allow_html=True,
            )

    with right:
        st.markdown("#### Relevant Events")
        if not data["relevant_events"]:
            st.caption("No relevant upcoming events.")
        for event in data["relevant_events"]:
            # Registration availability only -- schedule conflicts are checked in a mission.
            note = "already registered" if event["already_registered"] else _AVAILABILITY.get(event["availability"], event["availability"])
            st.markdown(
                f"- **{event['title']}** ({event['category']}) · {presenters.fmt_when(event['start_at'])} &nbsp; "
                f"{theme.status_badge(note)}",
                unsafe_allow_html=True,
            )

        st.markdown("#### Existing Grievances")
        if not data["grievances"]:
            st.caption("No grievances filed.")
        for g in data["grievances"]:
            breach_note = []
            if g["response_breached"]:
                breach_note.append("response overdue")
            if g["resolution_breached"]:
                breach_note.append("resolution overdue")
            note = "; ".join(breach_note) if breach_note else "within SLA"
            st.markdown(
                f"- **{g['case_code']}** ({g['category']}, {g['priority']}) &nbsp; {theme.status_badge(g['status'])}  \n  {note}",
                unsafe_allow_html=True,
            )

    st.markdown("#### Calendar Activities")
    if not data["calendar"]:
        st.caption("No calendar entries yet.")
    for entry in data["calendar"]:
        st.markdown(f"- **{entry['title']}** ({entry['source_type']}) · {presenters.fmt_when(entry['start_at'], entry['end_at'])}")


def _render_active_missions(missions: list) -> None:
    """Missions still waiting on someone: a selection, an approval, a resume."""
    waiting = [
        m for m in missions
        if m["user_selection_required"] or m["pending_approvals"] or m["status"] in ("needs_approval", "in_progress", "planning")
    ]
    st.markdown("#### Active missions & actions")
    if not waiting:
        recent = f" {presenters.plural(len(missions), 'recent mission')}, all finished." if missions else ""
        st.caption("Nothing is waiting on you." + recent)
        return
    for mission in waiting[:5]:
        if mission["user_selection_required"]:
            state = "USER SELECTION REQUIRED"
        elif mission["pending_approvals"]:
            state = "WAITING FOR APPROVAL"
        else:
            state = mission["status"]
        with st.container(border=True):
            row = st.columns([6, 2, 2])
            row[0].markdown(f"**{mission['goal']}**  \n<span class='cn-muted'>{presenters.fmt_when(mission['created_at'])}</span>", unsafe_allow_html=True)
            row[1].markdown(theme.status_badge(state), unsafe_allow_html=True)
            row[2].button("Open", key=f"open-{mission['mission_id']}", on_click=_open_mission, args=(mission["mission_id"],))
