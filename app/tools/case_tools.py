"""The ``create_campus_case`` write tool.

A complaint has no natural dedup key -- two identical-looking submissions
are still two distinct real-world complaints -- so idempotency here rests
entirely on the ``ToolGateway``'s idempotency-key short-circuit (a retried
call with the same key never reaches this handler a second time), not on
any dedup query inside the handler itself.

Also creates the case's ``CaseSLA`` row in the same transaction, using the
priority-based windows from ``app/rules/sla.py`` (sourced from the
Grievance SLA Policy) -- a filed case is not fully real without SLA
tracking, and a case with no ``CaseSLA`` row would make the existing,
unmodified Campus Services Agent read path correctly report it
``NEEDS_REVIEW`` ("SLA data missing") on any later read.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.models.identity import Student
from app.db.models.services import CampusCase, CasePriority, CaseSLA, CaseStatus
from app.rules.sla import sla_windows_for_priority
from app.schemas.action import CreateCampusCaseInput
from app.schemas.enums import ToolExecutionStatus
from app.schemas.tools import ToolResult


def create_campus_case(session: Session, data: CreateCampusCaseInput, tool_call_id: str) -> ToolResult:
    student = session.execute(select(Student).where(Student.student_code == data.student_id)).scalar_one_or_none()
    if student is None:
        return ToolResult(tool_call_id=tool_call_id, status=ToolExecutionStatus.FAILED, error=f"unknown student_id {data.student_id}")

    # Deliberately non-sequential (unlike the seeded CASE-000N codes) to stay
    # race-free without a shared counter -- see app/rules/action_preconditions.py.
    case_code = f"CASE-{uuid.uuid4().hex[:10].upper()}"
    created_at = utc_now()
    case = CampusCase(
        case_code=case_code,
        student_id=student.id,
        category=data.category,
        description=data.description,
        priority=CasePriority(data.priority),
        department=data.department,
        status=CaseStatus.OPEN,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(case)
    session.flush()  # assigns case.id, needed for the CaseSLA FK below

    response_hours, resolution_hours = sla_windows_for_priority(data.priority)
    sla = CaseSLA(
        case_id=case.id,
        response_due_at=created_at + timedelta(hours=response_hours),
        resolution_due_at=created_at + timedelta(hours=resolution_hours),
    )
    session.add(sla)
    session.commit()
    session.refresh(case)
    return ToolResult(
        tool_call_id=tool_call_id,
        status=ToolExecutionStatus.SUCCESS,
        data={"case_code": case.case_code, "status": case.status.value},
    )
