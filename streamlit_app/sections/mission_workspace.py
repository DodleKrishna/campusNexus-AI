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
    (
        "📝 Workshop registration (approval-gated)",
        "Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my classes "
        "or exams, and register me for it.",
    ),
]

_MISSION_ID_KEY = "cn_current_mission_id"


def render(client: ApiClient, student_id: str) -> None:
    st.subheader("Mission Workspace")
    st.caption(
        "State a goal. CampusNexus plans it, dispatches specialist agents, verifies the results, and "
        "pauses for human approval before any action. Demo environment: data is a fictional seeded campus, "
        "and approved actions write only to the local demo database. No real institutional system is contacted."
    )

    cols = st.columns(len(CANNED_MISSIONS))
    for i, (label, goal) in enumerate(CANNED_MISSIONS):
        if cols[i].button(label, use_container_width=True, key=f"canned-{i}"):
            _run_mission(client, student_id, goal)

    with st.form("cn-custom-goal-form", clear_on_submit=True):
        custom_goal = st.text_area("Or describe your own goal", placeholder="e.g. Register me for the Competitive Coding Contest...")
        submitted = st.form_submit_button("Run Mission")
    if submitted and custom_goal.strip():
        _run_mission(client, student_id, custom_goal.strip())

    with st.expander("Open an existing mission (e.g. to resume it after an approval)"):
        existing_id = st.text_input("Mission ID", placeholder="mission-…", key="cn-open-mission-id")
        if st.button("Open mission", key="cn-open-mission") and existing_id.strip():
            st.session_state[_MISSION_ID_KEY] = existing_id.strip()

    mission_id = st.session_state.get(_MISSION_ID_KEY)
    if mission_id:
        st.divider()
        _render_mission(client, mission_id)


_STATUS_GUIDANCE = {
    "needs_approval": (
        "info",
        "⏸️ Paused for human approval. A Campus Administrator must approve or reject the proposed action in the "
        "**Action Center**. Then come back here and click **Resume after approval** to execute it and run the "
        "post-execution check.",
    ),
    "failed": (
        "error",
        "❌ This mission did not complete. The final response, agent errors and timeline below explain why.",
    ),
    "needs_replan": ("warning", "⚠️ A task failed and the mission is waiting to be replanned."),
    "in_progress": ("info", "⏳ This mission is still in progress."),
    "planning": ("info", "⏳ This mission is still being planned."),
}


def _run_mission(client: ApiClient, student_id: str, goal: str) -> None:
    try:
        with st.spinner("Planning, dispatching agents and verifying results (live-LLM mode can take up to a minute)..."):
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
    guidance = _STATUS_GUIDANCE.get(mission["status"])
    if guidance is not None:
        getattr(st, guidance[0])(guidance[1])

    st.markdown("#### Structured Execution Plan")
    if not mission["plan"]:
        st.caption("No tasks were planned for this goal. See the final response below.")
    short_ids = {task["task_id"]: f"T{index}" for index, task in enumerate(mission["plan"], start=1)}
    for task in mission["plan"]:
        deps = (
            " — uses results from " + ", ".join(short_ids.get(d, d) for d in task["dependencies"])
            if task["dependencies"]
            else " — independent"
        )
        st.markdown(
            f"- **{short_ids[task['task_id']]}** · {_agent_label(task['agent'])}: {task['objective']}{deps} &nbsp; "
            f"{theme.status_badge(task['status'])}",
            unsafe_allow_html=True,
        )
    agents_involved = sorted({t["agent"] for t in mission["plan"]})
    if agents_involved:
        st.markdown("**Participating agents:** " + ", ".join(_agent_label(a) for a in agents_involved))

    st.markdown("#### Agent Execution Results")
    if not mission["agent_results"]:
        st.caption("No agent has run yet.")
    runs_per_task: dict = {}
    for run in mission["agent_results"]:
        runs_per_task[run["task_id"]] = runs_per_task.get(run["task_id"], 0) + 1
    attempt_seen: dict = {}
    for run in mission["agent_results"]:
        label = short_ids.get(run["task_id"], run["task_id"])
        attempt_seen[run["task_id"]] = attempt_seen.get(run["task_id"], 0) + 1
        if runs_per_task[run["task_id"]] > 1:
            # A task runs again after an approval (resume) or a replan; each
            # run is a separate, persisted record -- label it, don't hide it.
            label += f" (run {attempt_seen[run['task_id']]} of {runs_per_task[run['task_id']]})"
        with st.expander(f"{label} · {_agent_label(run['agent'])} · {run['status'].upper()}"):
            st.markdown(theme.status_badge(run["status"]), unsafe_allow_html=True)
            if run["errors"]:
                st.warning("; ".join(run["errors"]))
            if run.get("response_text"):
                st.markdown(run["response_text"])
            st.caption("Structured facts passed to the orchestrator and to any dependent task:")
            st.json(run["facts"], expanded=False)

    if mission["pending_approvals"]:
        st.markdown("#### Recommended Actions (waiting for approval)")
        for approval in mission["pending_approvals"]:
            st.info(f"**{approval['action_summary']}**: open the Action Center to approve or reject.")

    st.markdown("#### Evidence & Trust")
    _render_evidence(client, mission_id, short_ids)

    if mission.get("final_result"):
        st.markdown("#### Final Response")
        _render_final_response(mission["status"], mission["final_result"])

    with st.expander("Mission timeline"):
        _render_timeline(client, mission_id)


_AGENT_LABELS = {
    "academic_agent": "🎓 Academic Agent",
    "career_agent": "💼 Career Agent",
    "events_opportunity_agent": "📅 Events & Opportunity Agent",
    "campus_services_agent": "🏢 Campus Services Agent",
    "action_agent": "⚙️ Action Agent",
    "knowledge_rag_agent": "📚 Knowledge Agent",
}


def _agent_label(agent: str) -> str:
    return _AGENT_LABELS.get(agent, agent)


def _render_final_response(status: str, text: str) -> None:
    """Colour the final response by the mission's real outcome -- a failed or
    paused mission must never be shown in a green success box."""
    if status == "completed":
        st.success(text)
    elif status == "failed":
        st.error(text)
    elif status in ("needs_approval", "needs_replan"):
        st.warning(text)
    else:
        st.info(text)


def _render_evidence(client: ApiClient, mission_id: str, short_ids: dict) -> None:
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
        count = len(task_evidence["evidence"])
        task_label = short_ids.get(task_evidence["task_id"], task_evidence["task_id"])
        with st.expander(
            f"{task_label} · {_agent_label(task_evidence['agent'])} · {count} policy citation{'s' if count != 1 else ''}"
        ):
            for index, ev in enumerate(task_evidence["evidence"], start=1):
                st.markdown(f"**[{index}] {ev['title']}** ({ev.get('policy_version') or 'unversioned'})")
                meta = [f"document `{ev['document_id']}`"]
                if ev.get("section"):
                    meta.append(f"section: {ev['section']}")
                if ev.get("relevance_score") is not None:
                    meta.append(f"retrieval relevance {ev['relevance_score']:.2f}")
                st.caption(" · ".join(meta))
                st.markdown("> " + ev["snippet"].strip().replace("\n", "\n> "))


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
