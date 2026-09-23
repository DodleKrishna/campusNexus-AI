"""Light Streamlit UI smoke tests (Phase 8 §14 -- deliberately not exhaustive;
see docs/ARCHITECTURE.md's Phase 8 section for the scoping rationale: test
depth goes to the API layer, this proves the screens render against real
data without exceptions).

Runs streamlit_app/app.py in-process via streamlit.testing.v1.AppTest,
pointed at the real FastAPI app through an ASGI transport -- no real server,
no network, fully offline per CLAUDE.md's testing rules.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from streamlit_app import api_client as api_client_module

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

APP_PATH = str(Path(__file__).resolve().parents[1] / "streamlit_app" / "app.py")


@pytest.fixture()
def offline_transport(api_app):
    """Points every ApiClient built during this test at the real FastAPI app
    in-process (no server, no network) -- see api_client.py's module docstring."""
    with TestClient(api_app) as client:
        api_client_module.set_default_http_client(client)
        yield client
    api_client_module.set_default_http_client(None)


def test_app_loads_without_exception(offline_transport) -> None:
    at = AppTest.from_file(APP_PATH)
    at.run(timeout=30)
    assert not at.exception


def test_dashboard_renders_student_data(offline_transport) -> None:
    at = AppTest.from_file(APP_PATH)
    at.run(timeout=30)
    assert not at.exception
    # Default identity is student-demo; default page is Dashboard.
    assert any("Campus Dashboard" in sh.value for sh in at.subheader)
    body_text = "\n".join(md.value for md in at.markdown)
    assert "Attendance Overview" in body_text
    assert "Existing Grievances" in body_text


def test_switching_to_admin_identity_unlocks_campus_operations(offline_transport) -> None:
    at = AppTest.from_file(APP_PATH)
    at.run(timeout=30)
    assert not at.exception

    at.sidebar.selectbox[0].set_value("admin-demo").run(timeout=30)
    assert not at.exception
    radio_options = at.sidebar.radio[0].options
    assert "Campus Operations" in radio_options


def test_action_center_renders_without_exception(offline_transport) -> None:
    at = AppTest.from_file(APP_PATH)
    at.run(timeout=30)
    assert not at.exception

    at.sidebar.radio[0].set_value("Action Center").run(timeout=30)
    assert not at.exception
