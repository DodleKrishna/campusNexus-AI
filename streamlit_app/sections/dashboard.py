"""Campus Dashboard section (Phase 8 §5) -- real database-backed information
for the selected student, composed entirely from GET /students/{id}/dashboard
and GET /students/{id}/calendar. No numbers are computed or invented here.
"""
from __future__ import annotations

import streamlit as st

from streamlit_app import theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError


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

    student = data["student"]
    col1, col2, col3 = st.columns(3)
    with col1:
        theme.render_metric_card("Student", student["full_name"], f"{student['student_code']} · Year {student['year']}, Sem {student['semester']}")
    with col2:
        theme.render_metric_card("CGPA", f"{student['cgpa']:.2f}", student["department_code"])
    with col3:
        theme.render_metric_card("Open Grievances", str(len(data["grievances"])))

    st.markdown("#### Attendance Overview")
    if not data["attendance"]:
        st.caption("No attendance records found.")
    for item in data["attendance"]:
        with st.container(border=True):
            cols = st.columns([3, 2, 3])
            cols[0].markdown(f"**{item['course_title']}** ({item['course_code']})")
            cols[1].write(f"{item['classes_attended']}/{item['classes_conducted']} classes")
            if item["current_percentage"] is not None:
                badge = theme.status_badge("verified" if item["eligible_now"] else "failed")
                cols[2].markdown(
                    f"{item['current_percentage']}% (required {item['required_percentage']}%) &nbsp; {badge}",
                    unsafe_allow_html=True,
                )
            else:
                cols[2].markdown(f'<span class="cn-muted">policy threshold: {item["threshold_status"]}</span>', unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        st.markdown("#### Upcoming Examinations")
        if not data["upcoming_exams"]:
            st.caption("No upcoming exams.")
        for exam in data["upcoming_exams"]:
            st.markdown(f"- **{exam['course_title']}** ({exam['course_code']}) — {exam['exam_type']}  \n  {exam['scheduled_start']} · {exam['location']}")

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
            note = "already registered" if event["already_registered"] else event["availability"]
            st.markdown(
                f"- **{event['title']}** ({event['category']}) — {event['start_at']}  \n  {theme.status_badge(note)}",
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
        st.markdown(f"- **{entry['title']}** ({entry['source_type']}) — {entry['start_at']} → {entry['end_at']}")
