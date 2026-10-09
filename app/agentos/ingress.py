"""Nexus ingress: one deterministic classification of every assistant message, before any model is involved.

Text and voice reach Nexus through the same ``PersonalAssistant.handle_message``, so both use this router.

* ``GREETING`` -- "hi", "hello nexus", "good morning", "thanks": answered in code. No mission, no provider call, so it
  works online, offline and with every model unavailable.
* ``GROUNDED_CAMPUS_QUERY`` -- a campus-records question (``app.services.grounded_answers.classify``): a Nexus mission
  whose decisions are made deterministically by ``GroundedRoutingBrain`` (DB facts + organization knowledge). No AI
  decision is needed, so it does not require the configured brain to be reachable.
* ``MISSION_REQUEST`` -- planning / multi-step / monitoring requests: the full Nexus AgentOS mission (configured brain).
* ``UNSUPPORTED_ACTION`` -- a request to register, book, submit, pay, file...: the configured brain may consult the
  read-only specialists for candidates and explain the approval workflow, but nothing is executed from chat. When the
  brain is unavailable the user gets a fixed pointer to the workflow instead (no mission, nothing executed).
* ``GENERAL_CONVERSATION`` -- everything else: a Nexus mission with the configured brain. Its context is the kernel's
  small bounded ``AgentContext`` (this message, the mission's own observations); no chat or mission history.

The classification is keyword rules only. It never decides a campus fact and never authorizes anything.
"""
from __future__ import annotations

import enum
import re
from typing import Any, Callable, Optional

from app.schemas.enums import UserRole


class IngressIntent(str, enum.Enum):
    GREETING = "greeting"
    GROUNDED_CAMPUS_QUERY = "grounded_campus_query"
    MISSION_REQUEST = "mission_request"
    GENERAL_CONVERSATION = "general_conversation"
    UNSUPPORTED_ACTION = "unsupported_action"


# Intents answered without any model; the configured brain need not be available for them.
NO_BRAIN_INTENTS = frozenset({IngressIntent.GREETING, IngressIntent.GROUNDED_CAMPUS_QUERY})

_NAMES = r"(?:nexus|campus\s*ai|there|everyone|team|buddy|friend)"
_HELLO = r"(?:hi+|hii+|hello+|hey+|heya|hiya|yo|namaste|greetings|howdy|good\s+(?:morning|afternoon|evening|day))"
_GREETING = re.compile(rf"^(?:{_HELLO})(?:[\s,]+{_NAMES})?(?:[\s,]+(?:{_HELLO}))?[\s!.,?]*$")
_HOW_ARE_YOU = re.compile(rf"^(?:(?:{_HELLO})[\s,]+(?:{_NAMES}[\s,]+)?)?how\s+are\s+you(?:\s+doing)?(?:\s+today)?"
                          rf"(?:[\s,]+{_NAMES})?[\s!.,?]*$")
_THANKS = re.compile(rf"^(?:ok(?:ay)?[\s,]+)?(?:thanks+|thank\s+you|thx|ty|cheers)(?:\s+(?:a\s+lot|so\s+much|very\s+much))?"
                     rf"(?:[\s,]+{_NAMES})?[\s!.,]*$")
_BYE = re.compile(rf"^(?:bye+|goodbye|good\s+night|see\s+you|see\s+ya)(?:[\s,]+{_NAMES})?[\s!.,]*$")

# Requests to act on the student's behalf. Writes go only through the approval workflows (never from chat).
_ACTION = re.compile(
    r"\b(?:register\s+me|sign\s+me\s+up|enrol+\s+me|book\s+(?:me|a|an|the)\b|apply\s+(?:for\s+me|to|for\s+the)|"
    r"submit\s+my|file\s+a|raise\s+a|cancel\s+my|pay\s+(?:my|the)|send\s+(?:an?\s+)?(?:email|mail|message)\s+to|"
    r"call\s+(?:my|the)|request\s+leave|i\s+need\s+leave|mark\s+me\s+(?:present|absent))")
# Planning / multi-step / monitoring requests: a genuine agent mission.
_MISSION = re.compile(
    r"\b(?:plan\b|planning|help\s+me\s+(?:plan|prepare|organi[sz]e|get\s+ready)|prepare\s+(?:me|for)|"
    r"remind\s+me|keep\s+(?:track|an\s+eye)|monitor|follow\s+up|track\s+my|notify\s+me|watch\s+(?:for|my)|"
    r"step[-\s]by[-\s]step|roadmap|strategy|my\s+missions?|active\s+missions?)")

GREETING_REPLY = ("Hi! I'm Nexus. I can help with your classes, attendance, assignments, exams, placements, events "
                  "and campus services. What do you need?")
STAFF_GREETING_REPLY = ("Hi! I'm Nexus. I can help with your classes, attendance sessions, assignment submissions, "
                        "exams and campus requests. What do you need?")
HOW_ARE_YOU_REPLY = "I'm running well, thanks for asking! What can I help you with today?"
THANKS_REPLY = "You're welcome! Anything else I can help with?"
BYE_REPLY = "Goodbye! I'm here whenever you need me."
UNSUPPORTED_ACTION_REPLY = ("I can't carry out that action from chat. Registrations, bookings, submissions and requests "
                            "go through their approval workflow in the app, where you review and confirm them. I can "
                            "still answer questions about them.")


def _normalize(message: str) -> str:
    return " ".join((message or "").lower().replace("’", "'").split())


def greeting_reply(message: str, role: Optional[UserRole]) -> Optional[str]:
    """The fixed reply when ``message`` is small talk (a greeting, thanks or goodbye), else None."""
    text = _normalize(message)
    if not text or len(text) > 60:
        return None
    if _GREETING.match(text):
        return STAFF_GREETING_REPLY if role in (UserRole.FACULTY, UserRole.HOD) else GREETING_REPLY
    if _HOW_ARE_YOU.match(text):
        return HOW_ARE_YOU_REPLY
    if _THANKS.match(text):
        return THANKS_REPLY
    if _BYE.match(text):
        return BYE_REPLY
    return None


def classify_ingress(message: str, role: Optional[UserRole],
                     grounded_classify: Optional[Callable[[str, UserRole], Any]] = None) -> IngressIntent:
    """The ingress intent of ``message``. ``grounded_classify`` is the grounded-answer classifier when this deployment
    has grounded answers (otherwise no message is GROUNDED_CAMPUS_QUERY)."""
    if greeting_reply(message, role) is not None:
        return IngressIntent.GREETING
    text = _normalize(message)
    if _ACTION.search(text):
        return IngressIntent.UNSUPPORTED_ACTION
    if grounded_classify is not None and role is not None and grounded_classify(message, role) is not None:
        return IngressIntent.GROUNDED_CAMPUS_QUERY
    if _MISSION.search(text):
        return IngressIntent.MISSION_REQUEST
    return IngressIntent.GENERAL_CONVERSATION
