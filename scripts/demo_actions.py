"""Phase 7 Action Agent / Approval Gate / verified-execution demo runner.

Mirrors scripts/demo_multiagent.py's structure: runs against the real seeded
dev DB (STU-DEMO-001) and real Chroma policy store, with the offline
MockLLMProvider by default. Unlike the read-only Phase 6 demo, this one
*writes* real rows -- re-running it is safe and even instructive: the
registration/calendar demos naturally hit their own duplicate-prevention
checks on a second run (itself one of the required failure demonstrations),
and each complaint filed gets its own real case_code.

Usage:
    python scripts/demo_actions.py --student STU-DEMO-001
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.agents.action.agent import ActionAgent
from app.agents.events.agent import EventsAgent
from app.agents.services.agent import ServicesAgent
from app.db.repositories.calendar import get_calendar_entry
from app.db.repositories.cases import get_case
from app.db.repositories.events import get_registration
from app.db.repositories.missions import get_latest_approval_for_step
from app.db.session import create_db_engine, create_session_factory
from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.factory import get_llm_provider
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.schemas.enums import AgentName, ApprovalStatus, ToolAccessType, UserRole
from app.schemas.tools import ToolCall
from app.services import events as events_service
from app.services import services as services_service
from app.services.approval_gate import ApprovalGate
from app.services.context import ContextService
from app.services.knowledge import KnowledgeService
from app.tools.build import build_default_tool_registry

REGISTER_GOAL = (
    "Find the workshop titled 'Tech Talk: Cloud Native Systems', verify there are no conflicts with my "
    "classes or exams, and register me for it."
)
CALENDAR_GOAL = (
    "Create a personal calendar reminder titled 'Prepare for Cloud Native Systems Talk' for the workshop "
    "titled 'Tech Talk: Cloud Native Systems' using my calendar."
)
COMPLAINT_GOAL = "File a complaint: 'Hostel washroom tap still leaking after last repair.'"
REJECTED_COMPLAINT_GOAL = "File a complaint: 'Library projector bulb needs replacement.'"
EDITABLE_COMPLAINT_GOAL = "File a complaint: 'Fee installment plan needs review.'"
DUPLICATE_REGISTER_GOAL = (
    "Find the workshop titled 'Artificial Intelligence & Deep Learning Workshop', verify there are no "
    "conflicts with my classes or exams, and register me for it."
)
FULL_EVENT_GOAL = (
    "Find the workshop titled 'Startup Pitch Night', verify there are no conflicts with my classes or "
    "exams, and register me for it."
)


def _header(title: str) -> None:
    print(f"\n{'=' * 12} {title} {'=' * 12}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CampusNexus Phase 7 action demo runner")
    parser.add_argument("--student", default="STU-DEMO-001")
    parser.add_argument("--provider", default=None, help='LLM provider: "mock" (default) or "anthropic"')
    parser.add_argument("--admin", default="admin-demo", help="Simulated approving identity (not real auth -- see docs/ARCHITECTURE.md)")
    return parser.parse_args()


class Demo:
    def __init__(self, provider_name: Optional[str]) -> None:
        engine = create_db_engine()
        self.session_factory = create_session_factory(engine)

        config = get_rag_config()
        embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
        vector_store = PolicyVectorStore(path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider)
        retriever = PolicyRetriever(vector_store=vector_store, embedding_provider=embedding_provider)
        self.knowledge_service = KnowledgeService(retriever=retriever)

        self.llm_provider = get_llm_provider(provider_name)
        self.tool_gateway = build_default_tool_registry()

        registry = AgentRegistry()
        registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, lambda s: EventsAgent(session=s, knowledge_service=self.knowledge_service, llm_provider=self.llm_provider))
        registry.register(AgentName.CAMPUS_SERVICES_AGENT, lambda s: ServicesAgent(session=s, knowledge_service=self.knowledge_service, llm_provider=self.llm_provider))
        registry.register(AgentName.ACTION_AGENT, lambda s: ActionAgent(session=s, knowledge_service=self.knowledge_service, tool_gateway=self.tool_gateway))
        self.orchestrator = MissionOrchestrator(session_factory=self.session_factory, registry=registry, llm_provider=self.llm_provider)

    def action_agent(self, session) -> ActionAgent:
        return ActionAgent(session=session, knowledge_service=self.knowledge_service, tool_gateway=self.tool_gateway)

    # ------------------------------------------------------------------
    # Shared propose -> approve/reject -> resume flow
    # ------------------------------------------------------------------

    def run_action_mission(self, goal: str, student: str, admin: str, *, approve: bool, reason: Optional[str] = None):
        _header("MISSION GOAL")
        print(goal)

        final = self.orchestrator.run_mission(goal, user_id=student, user_role=UserRole.STUDENT, student_id=student)
        _header("GENERATED PLAN")
        for task in final["plan"].tasks:
            deps = f" (depends on: {', '.join(task.dependencies)})" if task.dependencies else " (independent)"
            print(f"- [{task.task_id}] {task.agent.value}: {task.objective}{deps}")
        print(f"\nMission status after first run: {final['mission_status'].value}")

        task_id = next((t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT), None)
        if task_id is None or task_id not in final["agent_results"] or "proposal" not in final["agent_results"][task_id].facts:
            _header("NO ACTION REACHED APPROVAL")
            result = final["agent_results"].get(task_id) if task_id else None
            print("issues:", result.errors if result else "(task never dispatched -- an upstream dependency failed or precheck failed outright)")
            return final, task_id

        proposal = final["agent_results"][task_id].facts["proposal"]
        _header("ACTION PROPOSAL (WAITING FOR APPROVAL)")
        print(json.dumps(proposal, indent=2, default=str))

        with self.session_factory() as session:
            approval = get_latest_approval_for_step(session, task_id)
            print(f"\napproval_id={approval.approval_id} status={approval.status.value}")
            gate = ApprovalGate(session)
            if approve:
                _header(f"EXPLICIT APPROVAL by {admin!r}")
                gate.decide(approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by=admin)
            else:
                _header(f"EXPLICIT REJECTION by {admin!r}")
                gate.decide(approval.approval_id, decision=ApprovalStatus.REJECTED, decision_by=admin, decision_reason=reason)

        _header("RESUMING MISSION (separate explicit operation)")
        final2 = self.orchestrator.resume_mission(final["mission_id"])
        print(f"Mission status after resume: {final2['mission_status'].value}")
        result = final2["agent_results"].get(task_id)
        if result is not None:
            print(f"Action Agent result: {result.status.value}; errors={result.errors}")
        return final2, task_id

    def audit_trail(self, mission_id: str) -> None:
        with self.session_factory() as session:
            context = ContextService(session)
            for event in context.list_audit_events(mission_id):
                print(f"- {event.event_type}: {event.message}")

    # ------------------------------------------------------------------
    # A. Workshop registration
    # ------------------------------------------------------------------

    def demo_a_workshop_registration(self, student: str, admin: str) -> Optional[str]:
        _header("DEMO A -- WORKSHOP REGISTRATION")
        with self.session_factory() as session:
            before = get_registration(session, 6, student)
            _header("DB STATE BEFORE")
            print(f"event_id=6 ('Tech Talk: Cloud Native Systems') registration for {student}: {before.status.value if before else '(none)'}")

        final, task_id = self.run_action_mission(REGISTER_GOAL, student, admin, approve=True)
        if task_id is None:
            return None

        with self.session_factory() as session:
            after = get_registration(session, 6, student)
            _header("DB STATE AFTER")
            print(f"event_id=6 registration for {student}: id={after.id if after else None} status={after.status.value if after else '(none)'}")

        _header("AUDIT TRAIL")
        self.audit_trail(final["mission_id"])
        return final["mission_id"]

    # ------------------------------------------------------------------
    # B. Calendar creation
    # ------------------------------------------------------------------

    def demo_b_calendar_creation(self, student: str, admin: str) -> None:
        _header("DEMO B -- CALENDAR CREATION")
        final, task_id = self.run_action_mission(CALENDAR_GOAL, student, admin, approve=True)
        if task_id is None:
            return
        with self.session_factory() as session:
            source_event = events_service.get_event_summary_by_title(session, "Tech Talk: Cloud Native Systems")
            expected_start = source_event.start_at - timedelta(hours=24)
            entry = get_calendar_entry(session, student, "Prepare for Cloud Native Systems Talk", expected_start)
            _header("PERSISTED DB STATE: CalendarEvent")
            print(f"title={entry.title if entry else None} start_at={entry.start_at if entry else None} source_type={entry.source_type if entry else None}")

    # ------------------------------------------------------------------
    # C. Campus complaint
    # ------------------------------------------------------------------

    def demo_c_campus_complaint(self, student: str, admin: str) -> None:
        _header("DEMO C -- CAMPUS COMPLAINT")
        with self.session_factory() as session:
            _header("CONTEXT: existing cases via Campus Services (read path)")
            for case in services_service.get_student_case_summaries(session, student):
                print(f"- {case.case_code}: {case.category} / {case.status} / {case.priority}")

        final, task_id = self.run_action_mission(COMPLAINT_GOAL, student, admin, approve=True)
        if task_id is None:
            return
        case_code = final["agent_results"][task_id].facts["tool_result"]["case_code"]
        with self.session_factory() as session:
            case = get_case(session, case_code)
            _header("PERSISTED DB STATE: CampusCase")
            print(f"case_code={case.case_code} category={case.category} status={case.status.value} department={case.department}")

    # ------------------------------------------------------------------
    # D. Failure scenarios
    # ------------------------------------------------------------------

    def demo_d_failures(self, student: str, admin: str) -> None:
        _header("DEMO D -- FAILURE SCENARIOS")

        _header("D1: Approval rejection")
        self.run_action_mission(REJECTED_COMPLAINT_GOAL, student, admin, approve=False, reason="Duplicate of an existing ticket.")

        _header("D2: Duplicate registration prevention (already registered for the AI workshop)")
        final = self.orchestrator.run_mission(DUPLICATE_REGISTER_GOAL, user_id=student, user_role=UserRole.STUDENT, student_id=student)
        print(f"mission_status={final['mission_status'].value}")
        task_id = next((t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT), None)
        if task_id and task_id in final["agent_results"]:
            print("errors:", final["agent_results"][task_id].errors)

        _header("D3: Full event (Startup Pitch Night, capacity 3, already full)")
        final = self.orchestrator.run_mission(FULL_EVENT_GOAL, user_id=student, user_role=UserRole.STUDENT, student_id=student)
        print(f"mission_status={final['mission_status'].value}")
        task_id = next((t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT), None)
        if task_id and task_id in final["agent_results"]:
            print("errors:", final["agent_results"][task_id].errors)

        _header("D4: Invalid student identity")
        final = self.orchestrator.run_mission(REGISTER_GOAL, user_id="STU-NOPE-999", user_role=UserRole.STUDENT, student_id="STU-NOPE-999")
        print(f"mission_status={final['mission_status'].value}")

        _header("D5: Unauthorized access (ownership mismatch at the Tool Gateway)")
        with self.session_factory() as session:
            call = ToolCall(
                tool_call_id="tc-demo-unauth", mission_id="m-demo", task_id="t-demo", tool_name="register_event",
                arguments={"student_id": "STU2023002", "event_id": 6}, read_or_write=ToolAccessType.WRITE,
                requires_approval=True, idempotency_key="k-demo-unauth",
            )
            result = self.tool_gateway.execute(session, call, caller_role=UserRole.STUDENT, caller_student_id=student)
            print(f"status={result.status.value} error={result.error}")

        _header("D6: Action modification after approval (propose_edit)")
        final = self.orchestrator.run_mission(EDITABLE_COMPLAINT_GOAL, user_id=student, user_role=UserRole.STUDENT, student_id=student)
        task_id = next((t.task_id for t in final["plan"].tasks if t.agent == AgentName.ACTION_AGENT), None)
        if task_id and task_id in final["agent_results"] and "proposal" in final["agent_results"][task_id].facts:
            print(f"original proposal_id={final['agent_results'][task_id].facts['proposal_id']}")
            with self.session_factory() as session:
                agent = self.action_agent(session)
                edited = agent.propose_edit(
                    final["mission_id"], task_id, student_id=student,
                    new_constraints={
                        "tool_name": "create_campus_case", "category": "fees",
                        "description": "Fee installment plan needs review -- amended amount and urgency.",
                        "priority": "high",
                    },
                    requested_by=admin, reason="Wrong priority on the original submission.",
                )
                print(f"edited proposal_id={edited.agent_result.facts.get('proposal_id')} status={edited.agent_result.status.value}")
                new_approval = get_latest_approval_for_step(session, task_id)
                ApprovalGate(session).decide(new_approval.approval_id, decision=ApprovalStatus.APPROVED, decision_by=admin)
            final3 = self.orchestrator.resume_mission(final["mission_id"])
            print(f"mission_status after edited approval + resume: {final3['mission_status'].value}")
            if task_id in final3["agent_results"]:
                case_code = final3["agent_results"][task_id].facts.get("tool_result", {}).get("case_code")
                if case_code:
                    with self.session_factory() as session:
                        persisted = get_case(session, case_code)
                        print(f"persisted case reflects the EDITED payload: category={persisted.category} priority={persisted.priority.value}")

        _header("D7: Postcondition verification independently catches a fabricated result")
        with self.session_factory() as session:
            agent = self.action_agent(session)
            verification = agent.verify_claimed_result("register_event", {"event_id": 999999, "student_id": student}, {})
            print(
                "claim: 'registration succeeded for a nonexistent event_id=999999' -- "
                f"independent post-check status={verification.status.value}; issues={verification.issues}"
            )


def main() -> int:
    args = parse_args()
    demo = Demo(args.provider)

    demo.demo_a_workshop_registration(args.student, args.admin)
    demo.demo_b_calendar_creation(args.student, args.admin)
    demo.demo_c_campus_complaint(args.student, args.admin)
    demo.demo_d_failures(args.student, args.admin)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
