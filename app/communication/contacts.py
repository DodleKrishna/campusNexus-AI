"""ContactResolver: the single place a student's phone number or e-mail address is read (AgentOS V2 Phase 5).

Only a ``DeliveryConnector`` may call it (anything else is refused with ``ContactAccessDenied``), immediately before a
delivery. The address lives only in the returned ``ResolvedContact`` in memory: it is never written to a job,
attempt, voice session, domain event, audit row, mission context or API response, and its ``repr`` is masked so
it cannot leak through logging. No AgentBrain, agent tool or API router imports this module
(``tests/test_agentos_communication.py`` enforces that).

Demo calls: only when ``CAMPUSNEXUS_DEMO_MODE=1`` AND ``CAMPUSNEXUS_TEST_PHONE`` is set, the one designated demo
student (``CAMPUSNEXUS_DEMO_CALL_STUDENT``, default ``STU-DEMO-001``) resolves to that phone number, in memory, at
delivery time. The number is read from the environment here and nowhere else; it is never written to the database,
a job, an attempt, an event, an audit row or telemetry, and no other student can ever resolve to it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.communication.base import DeliveryConnector
from app.db.models.communication_delivery import ContactKind, ContactPoint
from app.db.models.identity import Student, User


class ContactAccessDenied(PermissionError):
    """A caller other than a delivery connector asked for contact details."""


@dataclass(frozen=True)
class ResolvedContact:
    kind: ContactKind
    address: str = field(repr=False)
    verified: bool = False

    def __repr__(self) -> str:  # never print the address
        return f"ResolvedContact(kind={self.kind.value}, address=[redacted], verified={self.verified})"

    __str__ = __repr__


DEMO_MODE_ENV = "CAMPUSNEXUS_DEMO_MODE"
TEST_PHONE_ENV = "CAMPUSNEXUS_TEST_PHONE"
DEMO_CALL_STUDENT_ENV = "CAMPUSNEXUS_DEMO_CALL_STUDENT"
DEFAULT_DEMO_CALL_STUDENT = "STU-DEMO-001"
_PHONE = re.compile(r"^\+?[0-9]{10,15}$")


def demo_call_student_code() -> Optional[str]:
    """The designated demo student whose phone is the test phone, when demo calls are enabled; else None."""
    if (os.environ.get(DEMO_MODE_ENV) or "").strip() != "1" or demo_test_phone() is None:
        return None
    return (os.environ.get(DEMO_CALL_STUDENT_ENV) or DEFAULT_DEMO_CALL_STUDENT).strip() or None


def demo_test_phone() -> Optional[str]:
    raw = "".join((os.environ.get(TEST_PHONE_ENV) or "").split())
    return raw if _PHONE.match(raw) else None


class ContactResolver:
    def resolve(self, session: Session, connector: DeliveryConnector, student_id: int,
                kind: ContactKind) -> Optional[ResolvedContact]:
        """The student's verified address of ``kind`` in the caller's tenant session, or None. For e-mail, the
        institution address on the student's user record is the fallback (an institution-issued address)."""
        if not isinstance(connector, DeliveryConnector):
            raise ContactAccessDenied("CONTACT_ACCESS_DENIED")
        if kind == ContactKind.PHONE:
            demo_code = demo_call_student_code()
            if demo_code is not None:
                student = session.get(Student, student_id)  # tenant-filtered
                if student is not None and student.student_code == demo_code:
                    return ResolvedContact(kind, demo_test_phone() or "", True)
        point = session.execute(select(ContactPoint).where(
            ContactPoint.student_id == student_id, ContactPoint.kind == kind)).scalars().first()
        if point is not None and point.verified and point.address:
            return ResolvedContact(kind, point.address, True)
        if kind == ContactKind.EMAIL:
            student = session.get(Student, student_id)
            user = session.get(User, student.user_id) if student is not None else None
            if user is not None and user.email:
                return ResolvedContact(kind, user.email, True)
        return None
