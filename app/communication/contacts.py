"""ContactResolver: the single place a student's phone number or e-mail address is read (AgentOS V2 Phase 5).

Only a ``DeliveryConnector`` may call it (anything else is refused with ``ContactAccessDenied``), immediately before a
delivery. The address lives only in the returned ``ResolvedContact`` in memory: it is never written to a job,
attempt, voice session, domain event, audit row, mission context or API response, and its ``repr`` is masked so
it cannot leak through logging. No AgentBrain, agent tool or API router imports this module
(``tests/test_agentos_communication.py`` enforces that).
"""
from __future__ import annotations

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


class ContactResolver:
    def resolve(self, session: Session, connector: DeliveryConnector, student_id: int,
                kind: ContactKind) -> Optional[ResolvedContact]:
        """The student's verified address of ``kind`` in the caller's tenant session, or None. For e-mail, the
        institution address on the student's user record is the fallback (an institution-issued address)."""
        if not isinstance(connector, DeliveryConnector):
            raise ContactAccessDenied("CONTACT_ACCESS_DENIED")
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
