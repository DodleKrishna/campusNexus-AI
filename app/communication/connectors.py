"""Delivery connectors (AgentOS V2 Phase 5) and the deterministic message templates they send.

``InAppConnector`` is real and always available: it writes a student ``Notification`` (the existing in-app
notification table) in the caller's tenant transaction, and its post-check reloads that row. Message text comes
from fixed templates over ``SourceFacts`` (a title, course code and time) -- never from a model, never with a name
or contact detail. Further channels (e-mail, the Exotel voice provider) plug in through ``ConnectorRegistry``; one
that is not configured is simply unavailable and the policy never offers it.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.communication.base import DeliveryConnector, DeliveryRequest, DeliveryResult
from app.communication.sources import SourceFacts
from app.db.models.communication import Notification, NotificationStatus
from app.rules.communication_policy import DELIVERED_IN_APP, IN_APP

CATEGORY_PREFIX = "communication"
_TEMPLATES = {
    "submission_reminder": ("Reminder: {title}", "{title}{course} is due {when}. Please submit it before the deadline."),
    "deadline_warning": ("Deadline soon: {title}", "{title}{course} is due {when}. You have not submitted it yet."),
    "exam_reminder": ("Upcoming exam: {title}", "{title}{course} starts {when}. Please be on time."),
    "absence_followup": ("Missed exam: {title}", "You were marked absent for {title}{course} ({when}). "
                         "Please contact your faculty member about the next steps."),
    "absence_check": ("Absence recorded: {title}", "You were recorded absent from your {title} ({when}). "
                      "If this is wrong, or you had approved leave, please contact your faculty member."),
}
_DEFAULT = ("Message from your institution", "There is an update about {title}{course} ({when}).")


def compose_message(purpose: str, facts: SourceFacts) -> Tuple[str, str]:
    """(title, body) from a fixed template. Deterministic; the only inputs are the source's safe facts."""
    title_template, body_template = _TEMPLATES.get(purpose, _DEFAULT)
    course = f" ({facts.course_code})" if facts.course_code and facts.kind != "class" else ""
    values = {"title": facts.title, "course": course, "when": facts.when_text()}
    return title_template.format(**values)[:150], body_template.format(**values)


def category_for(purpose: str) -> str:
    return f"{CATEGORY_PREFIX}:{purpose}"[:60]


class InAppConnector(DeliveryConnector):
    channel, provider = IN_APP, "campusnexus_in_app"
    requires_internet = False  # a row in the local database: works offline
    REFERENCE_PREFIX = "notification:"

    def available(self) -> bool:
        return True  # needs no credentials or external provider

    def deliver(self, session: Session, request: DeliveryRequest) -> DeliveryResult:
        title, body = compose_message(request.purpose, request.facts)
        notification = Notification(student_id=request.student_id, title=title, body=body,
                                    category=category_for(request.purpose), status=NotificationStatus.SENT,
                                    created_at=request.now, sent_at=request.now)
        session.add(notification)
        session.flush()
        return DeliveryResult("delivered", provider_reference=f"{self.REFERENCE_PREFIX}{notification.id}",
                              outcome_code=DELIVERED_IN_APP)

    def verify(self, session: Session, request: DeliveryRequest, result: DeliveryResult) -> bool:
        reference = result.provider_reference or ""
        if result.status != "delivered" or not reference.startswith(self.REFERENCE_PREFIX):
            return False
        try:
            notification_id = int(reference[len(self.REFERENCE_PREFIX):])
        except ValueError:
            return False
        row = session.get(Notification, notification_id)  # tenant-filtered
        return (row is not None and row.student_id == request.student_id
                and row.category == category_for(request.purpose) and row.status == NotificationStatus.SENT)


class ConnectorRegistry:
    """The delivery connectors of this deployment, by channel. In-app is always present."""

    def __init__(self, connectors: Iterable[DeliveryConnector] = ()) -> None:
        self._by_channel: Dict[str, DeliveryConnector] = {IN_APP: InAppConnector()}
        for connector in connectors:
            self._by_channel[connector.channel] = connector

    def get(self, channel: Optional[str]) -> Optional[DeliveryConnector]:
        return self._by_channel.get(channel) if channel else None

    def available_channels(self) -> List[str]:
        return [channel for channel, connector in self._by_channel.items() if connector.available()]
