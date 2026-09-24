"""Enumerations shared across CampusNexus schema modules."""
from enum import Enum


class UserRole(str, Enum):
    """Role of the human on whose behalf a mission runs, or who is deciding an approval."""

    STUDENT = "student"
    FACULTY = "faculty"
    STAFF = "staff"
    ADMIN = "admin"


class MissionStatus(str, Enum):
    """Lifecycle status of a mission tracked in CampusState."""

    PENDING = "pending"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_REPLAN = "needs_replan"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    """Lifecycle status of a single MissionTask."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentName(str, Enum):
    """The frozen set of components that can be a task/message agent, source, or target."""

    MISSION_ORCHESTRATOR = "mission_orchestrator"
    ACADEMIC_AGENT = "academic_agent"
    CAREER_AGENT = "career_agent"
    EVENTS_OPPORTUNITY_AGENT = "events_opportunity_agent"
    CAMPUS_SERVICES_AGENT = "campus_services_agent"
    KNOWLEDGE_RAG_AGENT = "knowledge_rag_agent"
    ACTION_AGENT = "action_agent"


class AgentResultStatus(str, Enum):
    """Outcome status of an AgentResult handed back to the Orchestrator."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ToolAccessType(str, Enum):
    """Whether a tool is read-only (any agent may call it) or write/side-effecting
    (Action Agent only, per CLAUDE.md Tool Security Rules)."""

    READ = "read"
    WRITE = "write"


class ToolExecutionStatus(str, Enum):
    """Execution status of a ToolResult."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class ApprovalStatus(str, Enum):
    """Status of a human approval decision on a sensitive tool call."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDIT_REQUIRED = "edit_required"
    # Phase 11: an approval whose execution-time preconditions (or payload
    # binding) no longer hold. Terminal -- never reactivated, never executes;
    # a later valid action needs a brand-new approval. Not a human rejection.
    STALE = "stale"


class VerificationPhase(str, Enum):
    """Which side of execution a VerificationResult checks."""

    PRE_ACTION = "pre_action"
    POST_ACTION = "post_action"


class VerificationStatus(str, Enum):
    """Outcome status of a VerificationResult."""

    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
