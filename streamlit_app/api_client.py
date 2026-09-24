"""A thin wrapper around the CampusNexus FastAPI backend.

Every UI section talks to the backend only through this client -- never
imports app.db/app.services/app.graph directly, matching the objective's
"expose existing functionality rather than reproducing backend logic" for
the *UI* layer too (the API layer already does the real composition).

Test-only offline mode: ``set_default_http_client(fastapi.testclient.TestClient(app))``
points every subsequently-constructed ``ApiClient`` at the real FastAPI app
in-process -- no real server/socket, no network. A plain ``httpx.Client``
can't call an ASGI app synchronously on its own (``httpx.ASGITransport`` is
async-only); ``TestClient`` is what actually bridges that, and it's a real
``httpx.Client`` subclass, so it's a drop-in here.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_BASE_URL = os.environ.get("CAMPUSNEXUS_API_URL") or "http://127.0.0.1:8000"
# Planning + executing a mission makes several sequential LLM calls in live
# mode (planner, then each agent's classify/respond pair), so mission
# create/resume get a far longer budget than ordinary reads. A timeout here
# only means the *UI* stopped waiting -- the mission keeps running server-side.
MISSION_TIMEOUT_SECONDS = float(os.environ.get("CAMPUSNEXUS_UI_MISSION_TIMEOUT_SECONDS") or 300)

_default_http_client: Optional[httpx.Client] = None


def set_default_http_client(client: Optional[httpx.Client]) -> None:
    """Test-only seam -- see module docstring."""
    global _default_http_client
    _default_http_client = client


class ApiUnavailableError(Exception):
    """The backend could not be reached at all (connection refused/timeout)."""


class ApiError(Exception):
    """The backend responded with an error status."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class ApiClient:
    def __init__(
        self,
        identity_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        self._identity_key = identity_key
        effective_client = http_client if http_client is not None else _default_http_client
        if effective_client is not None:
            self._client = effective_client
            self._owns_client = False
        else:
            self._client = httpx.Client(base_url=base_url, timeout=timeout)
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self) -> Dict[str, str]:
        return {"X-Demo-Identity": self._identity_key}

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = self._client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.ConnectError as exc:
            raise ApiUnavailableError(f"Could not reach the CampusNexus API at {self._client.base_url}.") from exc
        except httpx.TimeoutException as exc:
            raise ApiUnavailableError(
                "The CampusNexus API did not respond in time. If a mission was running it may still finish "
                "server-side -- check the Action Center or re-open it before re-submitting the same goal."
            ) from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ApiError(response.status_code, str(detail))
        if response.status_code == 204:
            return None
        return response.json()

    # ------------------------------------------------------------------

    def get_health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    def list_demo_students(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/demo/students")

    def get_dashboard(self, student_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/students/{student_id}/dashboard")

    def get_calendar(self, student_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"/students/{student_id}/calendar")

    def create_mission(self, goal: str, student_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"goal": goal}
        if student_id is not None:
            body["student_id"] = student_id
        return self._request("POST", "/missions", json=body, timeout=MISSION_TIMEOUT_SECONDS)

    def get_mission(self, mission_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/missions/{mission_id}")

    def get_timeline(self, mission_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/missions/{mission_id}/timeline")

    def get_evidence(self, mission_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/missions/{mission_id}/evidence")

    def resume_mission(self, mission_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/missions/{mission_id}/resume", timeout=MISSION_TIMEOUT_SECONDS)

    def list_pending_approvals(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/approvals/pending")

    def decide_approval(self, approval_id: str, decision: str, reason: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"decision": decision}
        if reason:
            body["reason"] = reason
        return self._request("POST", f"/approvals/{approval_id}/decision", json=body)

    def list_admin_cases(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/admin/cases")
