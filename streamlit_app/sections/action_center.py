"""Action Center section (Phase 8 §9) -- the Approval Gate's UI.

Never auto-approves. Each approval card guards against duplicate
submissions from repeated clicks via a per-approval ``st.session_state``
flag set *before* the API call, so a second click on an already-decided
card is a no-op (the button disappears on rerun) rather than a second
POST -- the backend's own 409-on-already-resolved is the second line of
defense if a request somehow still lands twice.
"""
from __future__ import annotations

import streamlit as st

from streamlit_app import theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError


def render(client: ApiClient, can_decide: bool) -> None:
    st.subheader("Action Center")
    st.caption("Every pending action requires an explicit human decision -- nothing executes automatically.")

    try:
        approvals = client.list_pending_approvals()
    except ApiUnavailableError as exc:
        st.error(f"⚠️ {exc}")
        return
    except ApiError as exc:
        st.error(f"Could not load pending approvals ({exc.status_code}): {exc.detail}")
        return

    if not approvals:
        st.info("No pending approvals right now.")
        return

    for approval in approvals:
        _render_card(client, approval, can_decide)


def _render_card(client: ApiClient, approval: dict, can_decide: bool) -> None:
    approval_id = approval["approval_id"]
    decided_key = f"cn_decided_{approval_id}"
    already_decided = st.session_state.get(decided_key)

    with st.container(border=True):
        st.markdown(f"**{approval['action_summary']}**")
        info_cols = st.columns(4)
        info_cols[0].markdown(f"Tool  \n`{approval.get('tool_name') or 'n/a'}`")
        info_cols[1].markdown(f"Target resource  \n`{approval.get('target_resource') or 'n/a'}`")
        info_cols[2].markdown(f"Student  \n`{approval.get('student_id') or 'n/a'}`")
        info_cols[3].markdown(
            f"Verification  \n{theme.status_badge(approval.get('verification_status') or approval['status'])}",
            unsafe_allow_html=True,
        )

        with st.expander("Validated parameters & supporting evidence"):
            st.json(approval.get("parameters") or {})
            evidence = approval.get("evidence") or []
            if not evidence:
                st.caption("No policy evidence attached to this proposal.")
            for ev in evidence:
                st.markdown(f"- **{ev['title']}** (`{ev['document_id']}`): {ev['snippet'][:220]}")

        if not can_decide:
            st.caption("Only a Campus Administrator identity may approve or reject actions.")
        elif already_decided:
            st.caption(f"Decision submitted: **{already_decided}**. Switch tabs or refresh to see the updated list.")
        else:
            reason = st.text_input("Reason (recommended for a rejection)", key=f"cn_reason_{approval_id}")
            approve_col, reject_col = st.columns(2)
            if approve_col.button("✅ Approve", key=f"cn_approve_{approval_id}", use_container_width=True):
                _decide(client, approval_id, "approve", reason, decided_key)
            if reject_col.button("❌ Reject", key=f"cn_reject_{approval_id}", use_container_width=True):
                _decide(client, approval_id, "reject", reason, decided_key)


def _decide(client: ApiClient, approval_id: str, decision: str, reason: str, decided_key: str) -> None:
    st.session_state[decided_key] = decision  # set *before* the call: guards the very next rerun's click race
    try:
        client.decide_approval(approval_id, decision, reason or None)
        st.success(f"Action {decision}d.")
    except ApiUnavailableError as exc:
        st.session_state.pop(decided_key, None)
        st.error(f"⚠️ {exc}")
    except ApiError as exc:
        if exc.status_code == 409:
            st.warning("This action was already decided -- no duplicate decision was submitted.")
        else:
            st.session_state.pop(decided_key, None)
            st.error(f"Decision failed ({exc.status_code}): {exc.detail}")
    st.rerun()
