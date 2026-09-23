"""Default Tool Gateway registration -- mirrors app/graph/registry.py's
``AgentRegistry`` pattern: one ``register()`` call per tool, built once by
whichever caller (demo script, eval runner, test fixture, ActionAgent
constructor) needs a ready-to-use ``ToolGateway``.
"""
from __future__ import annotations

from app.schemas.enums import UserRole
from app.tools.calendar_tools import create_calendar_event
from app.tools.case_tools import create_campus_case
from app.tools.event_tools import register_event
from app.tools.registry import ToolDefinition, ToolGateway
from app.schemas.action import CreateCalendarEventInput, CreateCampusCaseInput, RegisterEventInput

_STUDENT_ONLY = frozenset({UserRole.STUDENT})


def build_default_tool_registry() -> ToolGateway:
    gateway = ToolGateway()

    gateway.register(
        ToolDefinition(
            name="register_event",
            input_model=RegisterEventInput,
            authorized_roles=_STUDENT_ONLY,
            required_permission="events:register_self",
            requires_approval=True,
            idempotency_strategy=(
                "idempotency_key = mission_id:step_id:register_event; natural dedup via "
                "EventRegistration's UniqueConstraint(event_id, student_id)"
            ),
            postconditions="exactly one CONFIRMED EventRegistration row for (event_id, student)",
            handler=register_event,
        )
    )
    gateway.register(
        ToolDefinition(
            name="create_calendar_event",
            input_model=CreateCalendarEventInput,
            authorized_roles=_STUDENT_ONLY,
            required_permission="calendar:write_self",
            requires_approval=True,
            idempotency_strategy=(
                "idempotency_key = mission_id:step_id:create_calendar_event; natural dedup via "
                "(student, title, start_at) lookup"
            ),
            postconditions="the expected CalendarEvent row exists and belongs to the student",
            handler=create_calendar_event,
        )
    )
    gateway.register(
        ToolDefinition(
            name="create_campus_case",
            input_model=CreateCampusCaseInput,
            authorized_roles=_STUDENT_ONLY,
            required_permission="services:file_case_self",
            requires_approval=True,
            idempotency_strategy="idempotency_key = mission_id:step_id:create_campus_case (no natural dedup key)",
            postconditions="the CampusCase exists, is associated with the student, and has status OPEN",
            handler=create_campus_case,
        )
    )
    return gateway
