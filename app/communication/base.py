"""Delivery connector contract (AgentOS V2 Phase 5).

A ``DeliveryConnector`` is the only code that performs a delivery, and the only code that may ask
``ContactResolver`` for a phone number or e-mail address. It receives a ``DeliveryRequest`` (ids, purpose and the
source's safe facts -- never contact details) and returns a structured ``DeliveryResult``. ``verify`` is the
deterministic post-condition check: it confirms from the database (or the provider's recorded state) that the
delivery really happened; the job is DELIVERED only when it does.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from sqlalchemy.orm import Session

from app.communication.sources import SourceFacts


@dataclass(frozen=True)
class DeliveryRequest:
    organization_id: int
    job_id: int
    attempt_id: int
    attempt_number: int
    student_id: int  # the recipient, by id only
    source_type: str
    purpose: str
    urgency: str
    facts: SourceFacts
    now: datetime


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["delivered", "in_progress", "failed"]  # in_progress: an asynchronous provider (a voice call)
    provider_reference: Optional[str] = None  # the provider's id for this attempt (never a contact detail)
    outcome_code: Optional[str] = None
    error_code: Optional[str] = None  # failed: a ``communication_policy`` code (retryable or not)


class DeliveryConnector(ABC):
    channel: str
    provider: str
    # Phase 6: a connector that needs the internet (voice/e-mail/SMS providers) is never attempted while the
    # deployment is offline; its job waits for connectivity instead (``WAITING_CONNECTIVITY``).
    requires_internet: bool = True

    @abstractmethod
    def available(self) -> bool:
        """Configured and usable (credentials present). Never makes a network call."""

    @abstractmethod
    def deliver(self, session: Session, request: DeliveryRequest) -> DeliveryResult:
        """Perform one delivery attempt. May stage rows in ``session`` (the caller's tenant transaction)."""

    @abstractmethod
    def verify(self, session: Session, request: DeliveryRequest, result: DeliveryResult) -> bool:
        """Deterministic post-condition: did the delivery reported in ``result`` really happen?"""
