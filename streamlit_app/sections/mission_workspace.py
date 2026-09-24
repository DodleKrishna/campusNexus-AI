"""Mission Workspace section (Phase 8 §6-§8; Phase 13 selection; Phase 14 polish).

Every field displayed comes straight from GET /missions/{id} (and /timeline,
/evidence, /candidates) -- real persisted execution state, never invented
"live" agent activity or fabricated confidence numbers. Readable labels and
summaries come from ``streamlit_app.presenters``, which only rephrases what
the API returned. Raw structured facts are shown only when "Show technical
details" is on in the sidebar.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import streamlit as st

from streamlit_app import presenters, theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError

# Demo starters only: each is sent as ordinary natural language through the
# same POST /missions path as a typed goal -- no special execution path.
CANNED_MISSIONS = [
    (
        "🎓 Attendance Recovery",
        "Check my Operating Systems attendance, determine whether I currently meet the attendance "
        "requirement, and explain how many classes I need to attend to reach the required attendance.",
    ),
    (
        "💼 AI Internship Preparation",
        "I'm a third-year CSE student interested in AI. Find internships I'm eligible for, identify my "
        "skill gaps, find relevant workshops that don't conflict with my classes, and create a preparation plan.",
    ),
    (
        "🗓️ Choose an Event & Register",
        "Find a suitable event, check my schedule and prepare my registration.",
    ),
    (
        "🏢 Check Overdue Grievances",
        "Check the status of my campus complaints, identify any overdue cases and explain the applicable "
        "grievance procedures.",
    ),
]

_MISSION_ID_KEY = "cn_current_mission_id"
_SELECTION_FLASH_KEY = "cn_selection_flash"
DEBUG_KEY = "cn_debug"
_LONG_RESPONSE_CHARS = 900

_STATE_STYLE = {
    "completed": "success",
    "user_selection": "info",
    "waiting_approval": "info",
    "needs_review": "warning",
    "provider_unavailable": "warning",
    "failed": "error",
    "running": "info",
}


def _debug() -> bool:
    return bool(st.session_state.get(DEBUG_KEY))


def render(client: ApiClient, student_id: str) -> None:
    st.subheader("Mission Workspace")
    st.caption(
        "State a goal. CampusNexus plans it, dispatches specialist agents, verifies the results, and pauses for "
        "your choice and for human approval before any action. Fictional seeded campus: approved actions write "
        "only to the local demo database."
    )

    cols = st.columns(len(CANNED_MISSIONS))
    for i, (label, goal) in enumerate(CANNED_MISSIONS):
        if cols[i].button(label, use_container_width=True, key=f"canned-{i}", help=goal):
            _run_mission(client, student_id, goal)

    with st.form("cn-custom-goal-form", clear_on_submit=True):
        custom_goal = st.text_area("Or describe your own goal", placeholder="e.g. Help me prepare for AI internships without missing important classes.")
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


# ---------------------------------------------------------------------------
# Running a mission, with progress read from real mission state
# ---------------------------------------------------------------------------


def _with_progress(call: Callable[[], dict], poll: Callable[[], str], label: str) -> dict:
    """Run a slow API call in a worker thread and, while it runs, show what the
    backend is actually doing (polled from persisted mission/step state)."""
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(call)
    try:
        with st.status(label, expanded=True) as box:
            last: Optional[str] = None
            while True:
                try:
                    result = future.result(timeout=0.7)
                    break
                except FutureTimeout:
                    try:
                        message = poll()
                    except (ApiUnavailableError, ApiError):
                        message = None
                    if message and message != last:
                        box.write(message)
                        last = message
            box.update(label="Done", state="complete", expanded=False)
        return result
    finally:
        pool.shutdown(wait=False)


def _run_mission(client: ApiClient, student_id: str, goal: str) -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=5)

    def poll() -> str:
        rows = client.list_student_missions(student_id, limit=3)
        row = next(
            (r for r in rows if r["goal"] == goal and (presenters._parse(r["created_at"]) or started) >= started),
            None,
        )
        return presenters.working_message(row)

    try:
        result = _with_progress(
            lambda: client.create_mission(goal, student_id=student_id), poll,
            "Working on your mission (live AI mode can take up to a minute)…",
        )
        st.session_state[_MISSION_ID_KEY] = result["mission_id"]
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"The mission could not be created ({exc.status_code}): {exc.detail}")
        return
    st.rerun()  # redraw without the finished progress box


def _resume(client: ApiClient, mission: dict) -> None:
    mission_id = mission["mission_id"]
    student_id = mission.get("student_id")

    def poll() -> str:
        rows = client.list_student_missions(student_id, limit=10) if student_id else []
        return presenters.working_message(next((r for r in rows if r["mission_id"] == mission_id), None))

    try:
        _with_progress(lambda: client.resume_mission(mission_id), poll, "Resuming the mission…")
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"Resume failed ({exc.status_code}): {exc.detail}")
        return
    st.rerun()


# ---------------------------------------------------------------------------
# Mission view
# ---------------------------------------------------------------------------


def _render_mission(client: ApiClient, mission_id: str) -> None:
    try:
        mission = client.get_mission(mission_id)
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"Could not load mission {mission_id} ({exc.status_code}): {exc.detail}")
        return

    state = presenters.mission_state(mission)
    head = st.columns([5, 2])
    head[0].markdown(f"### {mission['goal']}")
    head[0].markdown(
        f"{theme.status_badge(state.label)} &nbsp; <span class='cn-muted'>Mission {mission_id} · "
        f"updated {presenters.fmt_when(mission['updated_at'])}</span>",
        unsafe_allow_html=True,
    )
    # A planner-stage outage left no plan, so there is nothing to resume.
    can_resume = mission["status"] == "needs_approval" or (state.key == "provider_unavailable" and bool(mission["plan"]))
    if can_resume:
        label = "🔄 Resume mission" if state.key == "provider_unavailable" else "🔄 Resume after approval"
        if head[1].button(label, key=f"resume-{mission_id}", use_container_width=True):
            _resume(client, mission)

    stop = mission.get("execution_stop")
    if stop:
        # Phase 10: the Orchestrator stopped replanning a failure that repeated
        # exactly -- say so plainly instead of showing another generic failure.
        tasks = ", ".join(short_ids_for(mission).get(t, t) for t in stop.get("task_ids") or [])
        st.error(f"⏹️ {stop['message']}" + (f" (task {tasks})" if tasks else ""))
    else:
        getattr(st, _STATE_STYLE.get(state.key, "info"))(state.message)

    candidates = None
    if mission.get("user_selection_required") or mission.get("selected_target"):
        try:
            candidates = client.get_candidates(mission_id)
        except (ApiUnavailableError, ApiError) as exc:
            st.warning(f"Could not load candidate events: {exc}")

    st.markdown("#### Mission progress")
    st.markdown(theme.stage_strip(presenters.progress_stages(mission, candidates)), unsafe_allow_html=True)

    if candidates is not None:
        _render_candidate_selection(client, mission, candidates)

    _render_action_result(mission)

    if mission["pending_approvals"]:
        for approval in mission["pending_approvals"]:
            st.info(f"⏸️ **Waiting for approval:** {approval['action_summary']} Open the **Action Center** to approve or reject.")

    _render_stale_approvals(client, mission)

    short_ids = short_ids_for(mission)
    st.markdown("#### Execution plan")
    if not mission["plan"]:
        st.caption("No tasks were planned for this goal. See the final response below.")
    runs = presenters.latest_runs(mission)
    for task in mission["plan"]:
        deps = (
            " — uses results from " + ", ".join(short_ids.get(d, d) for d in task["dependencies"])
            if task["dependencies"] else " — independent"
        )
        label = presenters.task_label(task["agent"], (runs.get(task["task_id"]) or {}).get("facts"))
        st.markdown(
            f"- **{short_ids[task['task_id']]} · {label}** ({_agent_label(task['agent'])}){deps} &nbsp; "
            f"{theme.status_badge(task['status'])}  \n  <span class='cn-muted'>{task['objective']}</span>",
            unsafe_allow_html=True,
        )

    st.markdown("#### Agent results")
    if not mission["agent_results"]:
        st.caption("No agent has run yet.")
    for run, label in _run_cards(mission["agent_results"], short_ids):
        awaiting = bool(run["facts"].get("awaiting_approval"))
        verdict = "WAITING FOR APPROVAL" if awaiting else theme.verification_label(run["status"]).upper().replace("_", " ")
        title = presenters.task_label(run["agent"], run["facts"])
        with st.expander(f"{label} · {title} · {verdict}"):
            badge = theme.status_badge("WAITING_FOR_APPROVAL") if awaiting else theme.verification_badge(run["status"])
            st.markdown(f"Deterministic verifier: {badge} &nbsp; <span class='cn-muted'>{_agent_label(run['agent'])}</span>", unsafe_allow_html=True)
            summary = presenters.verification_summary(run, mission)
            if summary:
                st.caption(f"✔ {summary}")
            sources = presenters.evidence_sources(run.get("evidence") or [])
            if sources:
                st.caption("📚 Sources: " + "; ".join(sources))
            if run["errors"] and not awaiting:
                st.warning("; ".join(run["errors"]))
            if run.get("response_text"):
                st.markdown(run["response_text"])
            if _debug():
                st.caption("Structured facts passed to the orchestrator and to any dependent task:")
                st.json(run["facts"], expanded=False)

    st.markdown("#### Evidence & trust")
    _render_evidence(client, mission_id, short_ids)

    if mission.get("final_result"):
        st.markdown("#### Final response")
        text = mission["final_result"]
        if len(text) > _LONG_RESPONSE_CHARS:
            # The per-agent results above already carry each part; keep the
            # full synthesis one click away instead of a wall of text.
            with st.expander("Full written response", expanded=state.key == "failed"):
                _render_final_response(state.key, text)
        else:
            _render_final_response(state.key, text)

    with st.expander("Mission timeline (audit trail)"):
        _render_timeline(client, mission_id)


def _render_action_result(mission: dict) -> None:
    result = presenters.action_result(mission)
    if result is None:
        return
    st.markdown("#### Action result")
    rows = "".join(
        f"<div class='cn-kv'><span>{name}</span>{theme.status_badge(value)}</div>" for name, value in result.rows
    )
    target = f"<div class='cn-result-target'>{result.target}</div>" if result.target else ""
    note = f"<div class='cn-muted'>{result.note}</div>" if result.note else ""
    css = "cn-result-ok" if result.verified else "cn-result-bad"
    st.markdown(
        f"<div class='cn-card {css}'><div class='cn-result-head'>{'✅' if result.verified else '⛔'} {result.headline}</div>"
        f"<div class='cn-result-title'>{result.title}</div>{target}{rows}{note}</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Phase 13: candidate selection
# ---------------------------------------------------------------------------


def _render_candidate_selection(client: ApiClient, mission: dict, listing: dict) -> None:
    """The student picks the action's target from verified candidates.

    Every status and reason shown comes from the server's deterministic
    check. Only an eligible candidate gets an enabled button, and the server
    re-validates it again on click -- the UI never decides what is safe.
    """
    mission_id = mission["mission_id"]
    selected = listing.get("selected_target")
    flash = st.session_state.pop(_SELECTION_FLASH_KEY, None)

    if selected:
        st.markdown("#### Your selection")
        st.success(f"Selected: **{selected['title']}**")
        _render_continuation_progress(mission)
    if not listing["candidates"] or not (listing["user_selection_required"] or listing["selection_open"]):
        if flash is not None:
            getattr(st, flash[0])(flash[1])
        return

    selectable, blocked, not_open = presenters.split_candidates(listing["candidates"])
    st.markdown("#### Choose a different event" if selected else "#### Choose an event")
    st.caption(
        f"{len(selectable)} of {len(listing['candidates'])} events can be selected. CampusNexus recommends; you "
        "choose. Each event was checked against your timetable, exams and current registration rules. Nothing is "
        "registered until an administrator approves it."
    )
    if flash is not None:
        getattr(st, flash[0])(flash[1])
    if st.button("🔄 Refresh availability", key=f"refresh-candidates-{mission_id}"):
        try:
            client.refresh_candidates(mission_id)
        except (ApiUnavailableError, ApiError) as exc:
            st.session_state[_SELECTION_FLASH_KEY] = ("error", f"Refresh failed: {exc}")
        st.rerun()

    grid = st.columns(2)
    for index, candidate in enumerate(selectable + blocked):
        with grid[index % 2]:
            _render_candidate_card(client, mission_id, candidate, listing["selection_open"])
    if not_open:
        with st.expander(f"{presenters.plural(len(not_open), 'other event')} not open for registration yet"):
            for candidate in not_open:
                _render_candidate_card(client, mission_id, candidate, listing["selection_open"])


def _render_candidate_card(client: ApiClient, mission_id: str, candidate: dict, selection_open: bool) -> None:
    status = candidate["assessment"]["status"]
    with st.container(border=True):
        title = candidate["title"] + (" ✅" if candidate.get("selected") else "")
        st.markdown(f"**{title}** &nbsp; {theme.status_badge(status)}", unsafe_allow_html=True)
        when = presenters.fmt_when(candidate.get("start_at"), candidate.get("end_at"))
        st.caption(" · ".join(part for part in (when, candidate.get("venue")) if part))
        if candidate["selectable"]:
            seats = candidate.get("seats_remaining")
            st.markdown(
                f"🟢 {presenters.CANDIDATE_STATUS_TEXT['eligible']}"
                + (f" · {presenters.plural(seats, 'seat')} left" if seats is not None else "")
            )
        else:
            st.markdown(f"🔴 {presenters.CANDIDATE_STATUS_TEXT.get(status, status)}")
            # Only where it adds information beyond the status line above.
            reason = presenters.candidate_reason(candidate)
            if reason and status in ("conflict", "unavailable", "needs_review"):
                st.caption(reason)
        matched = candidate.get("matched_skill_gaps") or candidate.get("matched_terms")
        if candidate.get("matched_skill_gaps"):
            st.caption("Why recommended: matches your skill gap(s) " + ", ".join(matched))
        elif matched:
            st.caption("Why recommended: matches " + ", ".join(matched))
        if candidate.get("selected"):
            st.caption("Your selection")
            return
        key = f"select-{mission_id}-{candidate['resource_id']}"
        if candidate["selectable"] and selection_open:
            if st.button("Select this event", key=key, type="primary"):
                _select(client, mission_id, candidate)
        else:
            st.button("Unavailable", key=key, disabled=True, help="Selection disabled")


def _select(client: ApiClient, mission_id: str, candidate: dict) -> None:
    try:
        with st.spinner("Re-checking the event and preparing the registration…"):
            result = client.select_candidate(mission_id, candidate["resource_id"])
        st.session_state[_SELECTION_FLASH_KEY] = ("success", result["message"])
    except ApiUnavailableError as exc:
        st.session_state[_SELECTION_FLASH_KEY] = ("error", f"⚠️ {exc}")
    except ApiError as exc:
        # 409: the event changed since it was recommended -- the server says why.
        st.session_state[_SELECTION_FLASH_KEY] = ("warning", str(exc.detail))
    st.rerun()


def _render_continuation_progress(mission: dict) -> None:
    """Selected -> pre-check -> approval, read from the action task's real results."""
    runs = [r for r in mission["agent_results"] if r["agent"] == "action_agent"]
    latest = runs[-1] if runs else None
    steps = ["Selected"]
    if latest is not None:
        precheck = next((r["facts"].get("precheck_status") for r in reversed(runs) if r["facts"].get("precheck_status")), None)
        if precheck:
            steps.append(f"Pre-check {precheck.upper()}")
        if latest["facts"].get("awaiting_approval"):
            steps.append("Approval required")
        elif latest["status"] == "success":
            steps.extend(["Approved", "Registered and verified"])
        elif latest["status"] == "failed":
            steps.append("Blocked")
    st.markdown(" → ".join(f"**{step}**" for step in steps))


# ---------------------------------------------------------------------------
# Approvals, runs, evidence, timeline
# ---------------------------------------------------------------------------


def _render_stale_approvals(client: ApiClient, mission: dict) -> None:
    """Phase 11: approvals that expired because conditions changed after a human
    approved them. Never presented as a rejection; never reusable."""
    stale = mission.get("stale_approvals") or []
    if not stale:
        return
    st.markdown("#### Expired approvals")
    for approval in stale:
        replacement = approval.get("replaced_by_approval_id")
        follow_up = " A new approval request replaced it." if replacement else " Nothing was executed."
        st.warning(
            f"**{approval['action_summary']}**: Approval expired because execution conditions changed. Review the "
            f"updated action and approve again.{follow_up}"
        )
        if approval.get("invalidation_reason"):
            st.caption(f"What changed: {approval['invalidation_reason']}")
    awaiting_replacement = any(not a.get("replaced_by_approval_id") for a in stale)
    if awaiting_replacement and mission["status"] in ("failed", "needs_replan"):
        st.caption(
            "Once the conflicting change is resolved, re-check the action: if it is valid again, a NEW approval "
            "request is created (the expired one is never reused)."
        )
        if st.button("🔁 Re-check and request a new approval", key=f"recheck-{mission['mission_id']}"):
            _resume(client, mission)


def short_ids_for(mission: dict) -> dict:
    return {task["task_id"]: f"T{index}" for index, task in enumerate(mission["plan"], start=1)}


def _run_cards(runs: list, short_ids: dict) -> list:
    """One card per distinct run. A task runs again after an approval (resume)
    or a replan; each run is a separate persisted record, so it is labelled,
    never hidden -- but a run that repeated the previous one *exactly* (the
    API's ``identical_to_previous_run``) is folded into that card as an
    attempt count instead of a confusing identical duplicate."""
    cards: list = []  # [run, task_id, attempts]
    for run in runs:
        previous = next((c for c in reversed(cards) if c[1] == run["task_id"]), None)
        if run.get("identical_to_previous_run") and previous is not None:
            previous[2] += 1
            continue
        cards.append([run, run["task_id"], 1])

    per_task = {}
    for _, task_id, _ in cards:
        per_task[task_id] = per_task.get(task_id, 0) + 1
    seen: dict = {}
    labelled = []
    for run, task_id, attempts in cards:
        seen[task_id] = seen.get(task_id, 0) + 1
        label = short_ids.get(task_id, task_id)
        if per_task[task_id] > 1:
            label += f" (run {seen[task_id]} of {per_task[task_id]})"
        if attempts > 1:
            label += f" · same result on {attempts} attempts"
        labelled.append((run, label))
    return labelled


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


def _render_final_response(state_key: str, text: str) -> None:
    """Colour the final response by the mission's real outcome -- a failed or
    paused mission must never be shown in a green success box."""
    if state_key == "completed":
        st.success(text)
    elif state_key == "failed":
        st.error(text)
    elif state_key in ("waiting_approval", "needs_review", "provider_unavailable"):
        st.warning(text)
    else:
        st.info(text)


def _render_evidence(client: ApiClient, mission_id: str, short_ids: dict) -> None:
    try:
        evidence = client.get_evidence(mission_id)
    except (ApiUnavailableError, ApiError) as exc:
        st.warning(f"Could not load evidence: {exc}")
        return

    if not any(t["evidence"] for t in evidence["tasks"]):
        st.caption("No policy evidence was retrieved for this mission.")
        return

    seen_tasks = set()
    for task_evidence in evidence["tasks"]:
        # One block per task: a task that ran twice retrieved the same policies.
        if not task_evidence["evidence"] or task_evidence["task_id"] in seen_tasks:
            continue
        seen_tasks.add(task_evidence["task_id"])
        sources = presenters.evidence_sources(task_evidence["evidence"])
        task_label = short_ids.get(task_evidence["task_id"], task_evidence["task_id"])
        with st.expander(f"{task_label} · {_agent_label(task_evidence['agent'])} · Source: {', '.join(sources)}"):
            for index, ev in enumerate(task_evidence["evidence"], start=1):
                heading = f"**[{index}] {ev['title']}** ({ev.get('policy_version') or 'unversioned'})"
                if ev.get("section"):
                    heading += f" · {ev['section']}"
                st.markdown(heading)
                if _debug():
                    meta = [f"document `{ev['document_id']}`"]
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
            f"<span class='cn-muted'>{presenters.fmt_when(entry['timestamp'])}</span> &nbsp; "
            f"{theme.status_badge(entry['status'])} &nbsp; **{entry['event_type'].replace('_', ' ')}** — {entry['message']}",
            unsafe_allow_html=True,
        )
