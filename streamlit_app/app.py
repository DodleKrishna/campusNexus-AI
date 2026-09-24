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
# `streamlit run streamlit_app/app.py` puts streamlit_app/ first on sys.path, so a
# bare `import app` would find this very file instead of the backend `app`
# package. The repo root must come first, and a wrongly cached `app` module
# (a file, not a package) is dropped so the next import resolves correctly.
sys.path[:] = [str(REPO_ROOT)] + [p for p in sys.path if Path(p or ".").resolve() != REPO_ROOT]
if "app" in sys.modules and not hasattr(sys.modules["app"], "__path__"):
    del sys.modules["app"]

import streamlit as st

from app.api.identities import DEMO_IDENTITIES
from app.schemas.enums import UserRole
from streamlit_app import theme
from streamlit_app.api_client import ApiClient, ApiError, ApiUnavailableError
from streamlit_app.sections import action_center, campus_operations, dashboard, mission_workspace

st.set_page_config(page_title="CampusNexus AI", page_icon="🎓", layout="wide")
theme.inject_theme()

_PROVIDER_NAMES = {"groq": "Groq", "anthropic": "Anthropic"}

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

def _render_runtime_status(api: ApiClient) -> None:
    """Show the backend's *actual* LLM mode and readiness (GET /health), so
    deterministic mock output is never presented as live AI output."""
    try:
        health = api.get_health()
    except ApiUnavailableError:
        st.error("API offline. Start it with `uvicorn app.api.main:app --port 8000`.")
        return
    except ApiError as exc:
        st.warning(f"API health check failed ({exc.status_code}).")
        return

    llm = health.get("llm") or {}
    configured = health.get("live_ai_configured") or {}
    if llm.get("live"):
        provider = _PROVIDER_NAMES.get(str(llm.get("provider")), str(llm.get("provider")))
        st.markdown(
            f'<div class="cn-mode cn-mode-live"><b>LIVE AI MODE</b><br>Provider: {provider}<br>Model: {llm.get("model")}</div>',
            unsafe_allow_html=True,
        )
        st.caption("Plans and wording come from the live model; every rule result is still computed deterministically.")
    else:
        st.markdown(
            '<div class="cn-mode cn-mode-demo"><b>DETERMINISTIC DEMO MODE</b><br>Offline planner, no external AI is called.</div>',
            unsafe_allow_html=True,
        )
        if configured.get("groq") or configured.get("anthropic"):
            st.caption("A live AI key is configured, but this API was started in demo mode. Nothing switches automatically.")
        else:
            st.caption("Live AI is not configured (no GROQ_API_KEY). Demo mode runs fully offline and is fully supported.")
    if not (health.get("database") or {}).get("seeded", True):
        st.warning("Database is not seeded. Run `python scripts/reset_demo_env.py`.")
    if not (health.get("policy_store") or {}).get("available", True):
        st.warning("Policy store is empty, so answers will have no evidence citations. Run `python scripts/reset_demo_env.py`.")


with st.sidebar:
    st.divider()
    if st.session_state.get("cn_page") not in pages:
        st.session_state["cn_page"] = pages[0]
    page = st.radio("Navigate", pages, key="cn_page")
    st.divider()
    _render_runtime_status(client)
    st.caption(f"Signed in as **{_IDENTITY_LABELS.get(selected_key, selected_key)}**")
    st.toggle("Show technical details", key=mission_workspace.DEBUG_KEY, help="Raw structured facts and document ids, for developers and judges.")

theme.render_hero("CampusNexus AI", "From Campus Goals to Verified Actions.")
st.caption(
    f"👤 Viewing as **{_IDENTITY_LABELS.get(selected_key, selected_key)}**"
    + (f" · student `{student_id}`" if identity_def.role != UserRole.STUDENT else "")
    + " · Local demo: fictional campus data, no production authentication · LLM mode shown in the sidebar"
)

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
