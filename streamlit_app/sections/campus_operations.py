"""Campus Operations section (Phase 8 §10; Phase 14 readability) -- admin/faculty
view over GET /admin/cases + GET /approvals/pending. Read-only; no new automation.
"""
from __future__ import annotations

import streamlit as st

from streamlit_app import presenters, theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError


def render(client: ApiClient) -> None:
    st.subheader("Campus Operations")
    st.caption(
        "Due dates follow the per-priority response and resolution windows of the Grievance SLA Policy; "
        "breaches are computed deterministically from each case's timestamps."
    )

    try:
        cases = client.list_admin_cases()
        approvals = client.list_pending_approvals()
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"Could not load campus operations data ({exc.status_code}): {exc.detail}")
        return

    breached = [c for c in cases if c["response_breached"] or c["resolution_breached"]]
    open_cases = [c for c in cases if c["status"] in ("open", "in_progress")]
    # Only open cases need attention; a breach on a resolved/closed case is history.
    overdue = [c for c in breached if c["status"] in ("open", "in_progress")]

    cols = st.columns(4)
    with cols[0]:
        theme.render_metric_card("Open Grievances", str(len(open_cases)))
    with cols[1]:
        theme.render_metric_card("Open & SLA Breached", str(len(overdue)), f"{len(breached)} breached incl. resolved/closed")
    with cols[2]:
        theme.render_metric_card("Pending Approvals", str(len(approvals)))
    with cols[3]:
        theme.render_metric_card("Total Cases", str(len(cases)))

    if overdue:
        st.markdown("#### Breached items (needs attention)")
        for case in overdue:
            st.markdown(
                f"- {theme.status_badge('sla breached')} **{case['case_code']}** · {case['category']} · "
                f"{case['department']} · {case['priority']} priority: {_breach_text(case)}",
                unsafe_allow_html=True,
            )

    st.markdown("#### All cases: priority & SLA status")
    if not cases:
        st.caption("No campus cases found.")
    for case in cases:
        with st.container(border=True):
            row = st.columns([4, 2, 2, 4])
            row[0].markdown(
                f"**{case['case_code']}** · {case['student_code']}  \n"
                f"<span class='cn-muted'>{case['category']} · {case['department']}</span>",
                unsafe_allow_html=True,
            )
            row[1].markdown(theme.status_badge(case["priority"]), unsafe_allow_html=True)
            row[2].markdown(theme.status_badge(case["status"]), unsafe_allow_html=True)
            if case["response_breached"] or case["resolution_breached"]:
                row[3].markdown(theme.status_badge("sla breached") + " " + _breach_text(case), unsafe_allow_html=True)
            elif case["response_due_at"] is not None:
                row[3].markdown(
                    theme.status_badge("within sla")
                    + f" <span class='cn-muted'>resolve by {presenters.fmt_when(case['resolution_due_at'])}</span>",
                    unsafe_allow_html=True,
                )
            else:
                row[3].caption("no SLA record")

    st.markdown("#### Pending approvals")
    if not approvals:
        st.caption("No pending approvals.")
    for approval in approvals:
        st.markdown(f"- **{approval['action_summary']}** &nbsp; {theme.status_badge(approval['status'])}", unsafe_allow_html=True)


def _breach_text(case: dict) -> str:
    notes = []
    if case["response_breached"]:
        notes.append(f"response was due {presenters.fmt_when(case['response_due_at'])}")
    if case["resolution_breached"]:
        notes.append(f"resolution was due {presenters.fmt_when(case['resolution_due_at'])}")
    return "; ".join(notes)
