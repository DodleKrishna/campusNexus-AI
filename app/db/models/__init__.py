"""ORM model registry.

Importing this module registers every mapped class on ``Base.metadata`` --
required before calling ``init_db`` / ``Base.metadata.create_all``.
"""
from app.db.models.academic import (
    AttendanceRecord,
    Course,
    CourseStatus,
    Enrollment,
    Exam,
    TimetableSlot,
)
from app.db.models.career import (
    Application,
    ApplicationStatus,
    Company,
    Opportunity,
    OpportunityDepartment,
    OpportunityEligibleYear,
    OpportunitySkill,
    OpportunityStatus,
    OpportunityType,
    Skill,
    StudentSkill,
)
from app.db.models.auth import AuthAccount
from app.db.models.communication import (
    AccountNotification,
    CalendarEvent,
    Notification,
    NotificationStatus,
    StaffNotification,
)
from app.db.models.events import Club, Event, EventRegistration, EventStatus, RegistrationStatus
from app.db.models.faculty import (
    AttendanceMarkStatus,
    AttendanceSession,
    AttendanceSessionStatus,
    FacultyProfile,
    SessionAttendanceMark,
    TeachingAssignment,
)
from app.db.models.identity import Department, Student, User
from app.db.models.mission import (
    ActionCandidateRecord,
    AgentRun,
    ApprovalRecord,
    AuditLog,
    MemoryRecord,
    Mission,
    MissionStep,
    TargetSelectionRecord,
    ToolCallRecord,
)
from app.db.models.services import CampusCase, CaseAssignment, CasePriority, CaseSLA, CaseStatus
from app.db.models.workflow import (
    OperationAuditEvent,
    WorkflowRequest,
    WorkflowRequestStatus,
    WorkflowRequestType,
)

__all__ = [
    "AuthAccount",
    "AttendanceRecord",
    "Course",
    "CourseStatus",
    "Enrollment",
    "Exam",
    "TimetableSlot",
    "Application",
    "ApplicationStatus",
    "Company",
    "Opportunity",
    "OpportunityDepartment",
    "OpportunityEligibleYear",
    "OpportunitySkill",
    "OpportunityStatus",
    "OpportunityType",
    "Skill",
    "StudentSkill",
    "CalendarEvent",
    "Notification",
    "NotificationStatus",
    "StaffNotification",
    "AccountNotification",
    "Club",
    "Event",
    "EventRegistration",
    "EventStatus",
    "RegistrationStatus",
    "Department",
    "Student",
    "User",
    "ActionCandidateRecord",
    "AgentRun",
    "ApprovalRecord",
    "AuditLog",
    "MemoryRecord",
    "Mission",
    "MissionStep",
    "TargetSelectionRecord",
    "ToolCallRecord",
    "CampusCase",
    "CaseAssignment",
    "CasePriority",
    "CaseSLA",
    "CaseStatus",
    "AttendanceMarkStatus",
    "AttendanceSession",
    "AttendanceSessionStatus",
    "FacultyProfile",
    "SessionAttendanceMark",
    "TeachingAssignment",
    "OperationAuditEvent",
    "WorkflowRequest",
    "WorkflowRequestStatus",
    "WorkflowRequestType",
]
