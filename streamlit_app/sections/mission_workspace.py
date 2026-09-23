"""Mission Workspace section (Phase 8 §6, §7, §8) -- the centerpiece screen.

Every field displayed comes straight from GET /missions/{id} (and
/timeline, /evidence) -- real persisted execution state, never invented
"live" agent activity or fabricated confidence numbers.
"""
from __future__ import annotations

import streamlit as st

from streamlit_app import theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError

CANNED_MISSIONS = [
    (
        "🎓 Academic attendance recovery",
        "Check my Operating Systems attendance, determine whether I currently meet the attendance "
        "requirement, and explain how many classes I need to attend to reach the required attendance.",
    ),
    (
        "💼 AI internship & workshop prep",
        "I'm a third-year CSE student interested in AI. Find internships I'm eligible for, identify my "
        "skill gaps, find relevant workshops that don't conflict with my classes, and create a preparation plan.",
    ),
    (
        "🏢 Campus grievance & SLA assistance",
        "Check the status of my campus complaints, identify any overdue cases and explain the applicable "
        "grievance procedures.",
    ),
]

_MISSION_ID_KEY = "cn_current_mission_id"


def render(client: ApiClient, student_id: str) -> None:
    st.subheader("Mission Workspace")
    st.caption(
        "State a goal. CampusNexus plans it, dispatches specialist agents, verifies the results, and "
        "pauses for human approval before any real-world action -- nothing here is simulated."
    )

    cols = st.columns(3)
    for i, (label, goal) in enumerate(CANNED_MISSIONS):
        if cols[i].button(label, use_container_width=True, key=f"canned-{i}"):
            _run_mission(client, student_id, goal)

    with st.form("cn-custom-goal-form", clear_on_submit=True):
        custom_goal = st.text_area("Or describe your own goal", placeholder="e.g. Register me for the Competitive Coding Contest...")
        submitted = st.form_submit_button("Run Mission")
    if submitted and custom_goal.strip():
        _run_mission(client, student_id, custom_goal.strip())

    mission_id = st.session_state.get(_MISSION_ID_KEY)
    if mission_id:
        st.divider()
        _render_mission(client, mission_id)


def _run_mission(client: ApiClient, student_id: str, goal: str) -> None:
    try:
        with st.spinner("Planning and executing mission..."):
            result = client.create_mission(goal, student_id=student_id)
        st.session_state[_MISSION_ID_KEY] = result["mission_id"]
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
    except ApiError as exc:
        st.error(f"Mission could not be created ({exc.status_code}): {exc.detail}")


def _render_mission(client: ApiClient, mission_id: str) -> None:
    try:
        mission = client.get_mission(mission_id)
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"Could not load mission {mission_id} ({exc.status_code}): {exc.detail}")
        return

    st.markdown(f"### Mission `{mission_id}`")
    header_cols = st.columns([2, 2, 2, 2])
    header_cols[0].markdown(f"**Status**  \n{theme.status_badge(mission['status'])}", unsafe_allow_html=True)
    header_cols[1].markdown(f"**Created**  \n{mission['created_at']}")
    header_cols[2].markdown(f"**Updated**  \n{mission['updated_at']}")
    if mission["status"] == "needs_approval":
        if header_cols[3].button("🔄 Resume after approval", key=f"resume-{mission_id}"):
            try:
                client.resume_mission(mission_id)
            except ApiUnavailableError as exc:
                st.error(f"⚠️ {exc}")
            except ApiError as exc:
                st.error(f"Resume failed ({exc.status_code}): {exc.detail}")
            st.rerun()

    st.markdown(f"**Goal:** {mission['goal']}")

    st.markdown("#### Structured Execution Plan")
    for task in mission["plan"]:
        deps = f" — depends on {', '.join(task['dependencies'])}" if task["dependencies"] else " — independent"
        st.markdown(
            f"- `{task['task_id']}` **{task['agent']}**: {task['objective']}{deps} &nbsp; {theme.status_badge(task['status'])}",
            unsafe_allow_html=True,
        )
    agents_involved = sorted({t["agent"] for t in mission["plan"]})
    st.markdown("**Participating agents:** " + ", ".join(f"`{a}`" for a in agents_involved))

    st.markdown("#### Agent Execution Results")
    if not mission["agent_results"]:
        st.caption("No agent has run yet.")
    for run in mission["agent_results"]:
        with st.expander(f"{run['agent']} — {run['task_id']}  ·  {run['status']}"):
            st.markdown(theme.status_badge(run["status"]), unsafe_allow_html=True)
            if run["errors"]:
                st.warning("; ".join(run["errors"]))
            st.json(run["facts"])

    if mission["pending_approvals"]:
        st.markdown("#### Recommended Actions (waiting for approval)")
        for approval in mission["pending_approvals"]:
            st.info(f"**{approval['action_summary']}** — open the Action Center to approve or reject.")

    st.markdown("#### Evidence & Trust")
    _render_evidence(client, mission_id)

    if mission.get("final_result"):
        st.markdown("#### Final Response")
        st.success(mission["final_result"])

    with st.expander("Mission timeline"):
        _render_timeline(client, mission_id)


def _render_evidence(client: ApiClient, mission_id: str) -> None:
    try:
        evidence = client.get_evidence(mission_id)
    except (ApiUnavailableError, ApiError) as exc:
        st.warning(f"Could not load evidence: {exc}")
        return

    any_evidence = any(t["evidence"] for t in evidence["tasks"])
    if not any_evidence:
        st.caption("No policy evidence was retrieved for this mission.")
        return

    for task_evidence in evidence["tasks"]:
        if not task_evidence["evidence"]:
            continue
        with st.expander(f"{task_evidence['agent']} — {task_evidence['task_id']}  ·  {len(task_evidence['evidence'])} citation(s)"):
            for ev in task_evidence["evidence"]:
                st.markdown(f"**{ev['title']}** · `{ev['document_id']}` ({ev.get('policy_version') or 'unversioned'})")
                if ev.get("section"):
                    st.caption(ev["section"])
                st.write(ev["snippet"])
                st.divider()


def _render_timeline(client: ApiClient, mission_id: str) -> None:
    try:
        timeline = client.get_timeline(mission_id)
    except (ApiUnavailableError, ApiError) as exc:
        st.warning(f"Could not load timeline: {exc}")
        return

    for entry in timeline["entries"]:
        st.markdown(
            f"`{entry['timestamp']}` &nbsp; {theme.status_badge(entry['status'])} &nbsp; **{entry['event_type']}** — {entry['message']}",
            unsafe_allow_html=True,
        )
