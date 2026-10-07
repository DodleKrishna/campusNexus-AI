"""Developer-only live smoke check of the Nexus assistant on the Groq AgentBrain (AgentOS V2 Phase 2).

Sends one safe message as a seeded account through the real kernel (<= 3 transitions)
and prints the structured result. Never part of pytest. Without GROQ_API_KEY it prints
SKIPPED and exits 0. It records one mission in the target database (the isolated demo
DB by default -- run ``python scripts/reset_demo_env.py`` first).

    python scripts/agentos_nexus_smoke.py [--email student@campusnexus.local] [--db data/demo/campusnexus_demo.db]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.agentos.nexus import AssistantUnavailable, PersonalAssistant, register_nexus  # noqa: E402
from app.agentos.providers import build_agent_brain  # noqa: E402
from app.agentos.registry import AgentRegistry, ToolRegistry  # noqa: E402
from app.agentos.runtime import AgentRuntime, MissionActor  # noqa: E402
from app.db.models.auth import AuthAccount  # noqa: E402
from app.db.models.identity import Student  # noqa: E402
from app.db.models.organization import MembershipStatus, OrganizationMembership  # noqa: E402
from app.db.session import open_database  # noqa: E402
from app.db.system_session import open_system_session  # noqa: E402
from app.db.tenant_session import TenantSessionFactory  # noqa: E402

MESSAGE = "Who am I and what can you help me with?"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default="student@campusnexus.local")
    parser.add_argument("--db", default="data/demo/campusnexus_demo.db")
    args = parser.parse_args()
    if not os.environ.get("GROQ_API_KEY"):
        print("SKIPPED: GROQ_API_KEY is not set.")
        return 0
    if not Path(args.db).exists():
        print(f"SKIPPED: {args.db} does not exist (run scripts/reset_demo_env.py).")
        return 0

    engine = open_database(args.db)
    with open_system_session(engine) as s:
        row = s.execute(select(AuthAccount, OrganizationMembership).join(
            OrganizationMembership, OrganizationMembership.account_id == AuthAccount.id).where(
            AuthAccount.email == args.email, OrganizationMembership.status == MembershipStatus.ACTIVE)).first()
        if row is None:
            print(f"FAILED: no active membership for {args.email}.")
            return 1
        account, membership = row
        student_code = s.get(Student, membership.student_id).student_code if membership.student_id else None
        actor = MissionActor(organization_id=membership.organization_id, account_id=account.id, role=membership.role,
                             student_code=student_code, faculty_profile_id=membership.faculty_profile_id)

    agents, tools = AgentRegistry(), ToolRegistry()
    register_nexus(agents, tools)
    runtime = AgentRuntime(agents, tools, build_agent_brain("groq"))
    with TenantSessionFactory(engine).open_tenant_session(actor.organization_id) as session:
        try:
            reply = PersonalAssistant(runtime, max_transitions=3).handle_message(session, actor, MESSAGE)
        except AssistantUnavailable as exc:
            print(json.dumps({"result": "UNAVAILABLE", "code": exc.code,
                              "mission_id": exc.reply.mission_id if exc.reply else None}, indent=2))
            return 2
    print(json.dumps({"result": "OK", **reply.model_dump(mode="json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
