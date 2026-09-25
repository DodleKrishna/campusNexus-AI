"""Phase 19 -- regression tests for bugs fixed during hackathon stabilization.

Offline (mock LLM); reuses the Phase 18 app/client fixtures (time pinned).
"""
from __future__ import annotations

from app.db.tenancy import ensure_default_organization, sync_memberships
from app.auth.passwords import hash_password
from app.db.models.auth import AuthAccount
from app.schemas.enums import UserRole
from tests.test_phase18_admin_ops import ADMIN_GETS, PASSWORD, app, auth, client, clock  # noqa: F401 (fixtures)

STAFF = "staff@campusnexus.local"


def test_staff_is_never_routed_to_or_admitted_into_the_admin_console(client, session_factory) -> None:
    with session_factory() as session:
        session.add(AuthAccount(email=STAFF, password_hash=hash_password(PASSWORD), role=UserRole.STAFF,
                                display_name="Front Office Staff"))
        session.flush()
        sync_memberships(session, ensure_default_organization(session))  # Phase 22: authoritative membership
        session.commit()
    login = client.post("/auth/login", json={"email": STAFF, "password": PASSWORD})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["home_route"] == "/unsupported-role"

    headers = auth(client, STAFF)
    assert client.get("/auth/me", headers=headers).json()["home_route"] == "/unsupported-role"
    for path in ADMIN_GETS:
        assert client.get(path, headers=headers).status_code == 403, path
    for path in ("/faculty/dashboard", "/hod/dashboard", "/me/dashboard"):
        assert client.get(path, headers=headers).status_code == 403, path


def test_credentials_file_always_matches_the_seeded_password(tmp_path, monkeypatch) -> None:
    """An env-var reset used to leave an older dev_credentials.txt showing a password that no longer worked."""
    from app.auth.accounts import resolve_seed_password

    file = tmp_path / "dev_credentials.txt"
    monkeypatch.delenv("CAMPUSNEXUS_DEMO_PASSWORD", raising=False)
    generated, _ = resolve_seed_password(file)
    monkeypatch.setenv("CAMPUSNEXUS_DEMO_PASSWORD", "chosen-for-the-demo")
    assert resolve_seed_password(file) == ("chosen-for-the-demo", "$CAMPUSNEXUS_DEMO_PASSWORD")
    assert "password: chosen-for-the-demo" in file.read_text(encoding="utf-8")
    assert f"password: {generated}" not in file.read_text(encoding="utf-8")
    monkeypatch.delenv("CAMPUSNEXUS_DEMO_PASSWORD")
    assert resolve_seed_password(file)[0] == "chosen-for-the-demo"  # the file now drives later resets
