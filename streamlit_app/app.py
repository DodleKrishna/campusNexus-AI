"""CampusNexus AI -- Streamlit demo UI (Phase 8 §4).

Single-entry app: a persistent sidebar identity switcher + section
navigation, backed entirely by the FastAPI backend through ``api_client``
-- no direct app.db/app.services/app.graph imports here, so the UI can
never drift from what the API actually enforces (role/ownership checks,
approval gating, etc. all happen server-side, not in this file).

Run with: streamlit run streamlit_app/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st

from app.api.identities import DEMO_IDENTITIES
from app.schemas.enums import UserRole
from streamlit_app import theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError
from streamlit_app.sections import action_center, campus_operations, dashboard, mission_workspace

st.set_page_config(page_title="CampusNexus AI", page_icon="🎓", layout="wide")
theme.inject_theme()

_IDENTITY_LABELS = {
    "student-demo": "Student — Aditi Rao (STU-DEMO-001)",
    "student-alt": "Student — Rohan Mehta (STU2023002)",
    "faculty-demo": "Faculty — Prof. Meera Nair",
    "admin-demo": "Campus Administrator — Priya Desai",
}

if "cn_identity_key" not in st.session_state:
    st.session_state["cn_identity_key"] = "student-demo"

with st.sidebar:
    st.markdown("## CampusNexus AI")
    st.caption("🔓 Local demo mode — no production authentication. See docs/ARCHITECTURE.md.")

    identity_keys = list(DEMO_IDENTITIES.keys())
    selected_key = st.selectbox(
        "Viewing as",
        options=identity_keys,
        format_func=lambda k: _IDENTITY_LABELS.get(k, k),
        index=identity_keys.index(st.session_state["cn_identity_key"]),
    )
    st.session_state["cn_identity_key"] = selected_key
    identity_def = DEMO_IDENTITIES[selected_key]
    is_staff = identity_def.role in (UserRole.ADMIN, UserRole.FACULTY)

client = ApiClient(selected_key)

# Which student's data is being viewed/acted on.
if identity_def.role == UserRole.STUDENT:
    student_id = identity_def.student_id
else:
    try:
        available_students = client.list_demo_students()
    except (ApiUnavailableError, ApiError):
        available_students = []
    student_options = {s["student_id"]: s["display_name"] for s in available_students} or {"STU-DEMO-001": "STU-DEMO-001"}
    with st.sidebar:
        student_id = st.selectbox("Viewing student", options=list(student_options.keys()), format_func=lambda sid: student_options[sid])

pages = ["Dashboard", "Mission Workspace", "Action Center"]
if is_staff:
    pages.append("Campus Operations")

with st.sidebar:
    st.divider()
    page = st.radio("Navigate", pages)
    st.divider()
    st.caption(f"Signed in as **{_IDENTITY_LABELS.get(selected_key, selected_key)}**")

theme.render_hero("CampusNexus AI", "From Campus Goals to Verified Actions.")

try:
    if page == "Dashboard":
        dashboard.render(client, student_id)
    elif page == "Mission Workspace":
        mission_workspace.render(client, student_id)
    elif page == "Action Center":
        action_center.render(client, can_decide=(identity_def.role == UserRole.ADMIN))
    elif page == "Campus Operations":
        campus_operations.render(client)
except ApiUnavailableError as exc:
    st.error(f"⚠️ {exc}\n\nStart the API with: `uvicorn app.api.main:app --reload --port 8000`")
except ApiError as exc:
    st.error(f"The backend returned an unexpected error ({exc.status_code}): {exc.detail}")
