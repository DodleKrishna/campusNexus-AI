"""The explicit local-demo identity allowlist (Phase 8 §3).

There is no production authentication in this phase. A client sends an
opaque key (the ``X-Demo-Identity`` header) and the server resolves it
against this fixed, server-side allowlist -- the client never supplies a
role or student_id that the server trusts directly. STUDENT identities are
backed by real seeded ``Student`` rows (their display name is always
resolved live from the DB, never hardcoded, so it can't drift); FACULTY/
ADMIN identities need no DB row at all -- exactly Phase 7's ``"admin-demo"``
approver-string pattern, just formalized into a lookup table.

See ``docs/ARCHITECTURE.md``'s Phase 8 section for the documented trust
boundary: this is a demo convenience, not a substitute for real auth/SSO.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from app.db.tenancy import DEFAULT_ORGANIZATION_SLUG
from app.schemas.enums import UserRole


@dataclass(frozen=True)
class DemoIdentityDefinition:
    key: str
    role: UserRole
    student_id: Optional[str]  # None for non-student identities
    fallback_display_name: str  # used only when student_id is None (no DB row to resolve from)
    # Phase 22B: the organization is fixed here, server-side; the client never names it.
    organization_slug: str = DEFAULT_ORGANIZATION_SLUG


DEMO_IDENTITIES: Dict[str, DemoIdentityDefinition] = {
    "student-demo": DemoIdentityDefinition(
        key="student-demo", role=UserRole.STUDENT, student_id="STU-DEMO-001", fallback_display_name="Student"
    ),
    "student-alt": DemoIdentityDefinition(
        key="student-alt", role=UserRole.STUDENT, student_id="STU2023002", fallback_display_name="Student"
    ),
    "faculty-demo": DemoIdentityDefinition(
        key="faculty-demo", role=UserRole.FACULTY, student_id=None, fallback_display_name="Prof. Meera Nair (Faculty)"
    ),
    "admin-demo": DemoIdentityDefinition(
        key="admin-demo", role=UserRole.ADMIN, student_id=None, fallback_display_name="Priya Desai (Campus Administrator)"
    ),
}
