"""Campus Operations section (Phase 8 §10) -- admin/faculty view over
GET /admin/cases + GET /approvals/pending. Read-only; no new automation.
"""
from __future__ import annotations

import streamlit as st

from streamlit_app import theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError


def render(client: ApiClient) -> None:
    st.subheader("Campus Operations")

    try:
        cases = client.list_admin_cases()
        approvals = client.list_pending_approvals()
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"Could not load campus operations data ({exc.status_code}): {exc.detail}")
        return

    overdue = [c for c in cases if c["response_breached"] or c["resolution_breached"]]
    open_cases = [c for c in cases if c["status"] in ("open", "in_progress")]

    cols = st.columns(4)
    with cols[0]:
        theme.render_metric_card("Open Grievances", str(len(open_cases)))
    with cols[1]:
        theme.render_metric_card("Overdue Cases", str(len(overdue)))
    with cols[2]:
        theme.render_metric_card("Pending Approvals", str(len(approvals)))
    with cols[3]:
        theme.render_metric_card("Total Cases", str(len(cases)))

    st.markdown("#### Case Priority & SLA Status")
    if not cases:
        st.caption("No campus cases found.")
    for case in cases:
        with st.container(border=True):
            row = st.columns([3, 2, 2, 3])
            row[0].markdown(f"**{case['case_code']}** — {case['student_code']}  \n{case['category']} · {case['department']}")
            row[1].markdown(theme.status_badge(case["priority"]), unsafe_allow_html=True)
            row[2].markdown(theme.status_badge(case["status"]), unsafe_allow_html=True)
            breach_notes = []
            if case["response_breached"]:
                breach_notes.append("response overdue")
            if case["resolution_breached"]:
                breach_notes.append("resolution overdue")
            if breach_notes:
                row[3].markdown(theme.status_badge("failed") + " " + "; ".join(breach_notes), unsafe_allow_html=True)
            elif case["response_due_at"] is not None:
                row[3].markdown(theme.status_badge("verified") + " within SLA", unsafe_allow_html=True)
            else:
                row[3].caption("no SLA record")

    st.markdown("#### Pending Approvals")
    if not approvals:
        st.caption("No pending approvals.")
    for approval in approvals:
        st.markdown(f"- **{approval['action_summary']}** &nbsp; {theme.status_badge(approval['status'])}", unsafe_allow_html=True)
