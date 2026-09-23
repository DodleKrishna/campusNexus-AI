"""Phase 8 §12: genuine separate-process mission-recovery test.

Spawns scripts/_persistence_subprocess_helper.py as two real, independent
OS processes (via subprocess.run) against one isolated temp DB -- not two
MissionOrchestrator instances sharing this test's own interpreter (that
same-process variant already exists in
tests/test_action_orchestration.py::test_cross_process_approval_and_resumption).
Demonstrates: a mission pauses for approval, the initiating process exits
completely, a separate process approves and resumes it, the action executes
exactly once, and a repeated resumption creates no duplicate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.db.session import create_db_engine, create_session_factory, init_db
from app.rag.config import get_rag_config
from app.rag.embeddings import DeterministicHashEmbedding
from app.rag.ingest import ingest_policy_directory
from app.rag.vector_store import PolicyVectorStore
from scripts.seed_data import run_seed

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "_persistence_subprocess_helper.py"
DEMO_STUDENT = "STU-DEMO-001"


@pytest.fixture()
def isolated_db_env(tmp_path):
    """A freshly-seeded, isolated temp DB + Chroma store, and the env vars
    that point a genuinely separate subprocess at exactly this state."""
    db_path = tmp_path / "persistence.db"
    chroma_path = tmp_path / "chroma"

    engine = create_db_engine(db_path=db_path)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run_seed(session)

    config = get_rag_config()
    embedding_provider = DeterministicHashEmbedding()
    vector_store = PolicyVectorStore(path=chroma_path, collection_name="persistence_test_policies", embedding_provider=embedding_provider)
    ingest_policy_directory(config.policy_dir, vector_store=vector_store)

    env = {
        "CAMPUSNEXUS_DB_PATH": str(db_path),
        "CAMPUSNEXUS_VECTOR_STORE_PATH": str(chroma_path),
        "CAMPUSNEXUS_CHROMA_COLLECTION": "persistence_test_policies",
        "CAMPUSNEXUS_EMBEDDING_PROVIDER": "deterministic",
        "CAMPUSNEXUS_LLM_PROVIDER": "mock",
    }
    return env, session_factory


def _run_helper(env: dict, *args: str) -> dict:
    full_env = {**os.environ, **env}
    result = subprocess.run(
        [sys.executable, str(HELPER), *args],
        env=full_env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr[-4000:]!r}"
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert lines, f"no output from subprocess; stderr={result.stderr[-4000:]!r}"
    return json.loads(lines[-1])


def test_mission_survives_a_real_process_boundary(isolated_db_env) -> None:
    env, session_factory = isolated_db_env

    # Process A: propose the registration, pause for approval, then exit completely.
    propose_result = _run_helper(env, "propose", "--student", DEMO_STUDENT)
    assert propose_result["mission_status"] == "needs_approval"
    mission_id = propose_result["mission_id"]

    from app.db.repositories.missions import get_mission

    with session_factory() as session:
        mission = get_mission(session, mission_id)
        assert mission is not None
        assert mission.status.value == "needs_approval"

    # Process B: a completely separate process approves and resumes.
    resume_result = _run_helper(env, "approve-and-resume", "--mission-id", mission_id)
    assert resume_result["mission_status"] == "completed"

    from app.db.repositories.events import get_registration

    with session_factory() as session:
        registration = get_registration(session, 10, DEMO_STUDENT)  # event 10 = Competitive Coding Contest
        assert registration is not None
        assert registration.status.value == "confirmed"

    # Process C: repeated resumption (e.g. an operator re-running the resume
    # step) must never create a duplicate registration.
    second_resume_result = _run_helper(env, "approve-and-resume", "--mission-id", mission_id)
    assert "error" in second_resume_result or second_resume_result.get("mission_status") == "completed"

    from sqlalchemy import select

    from app.db.models.events import EventRegistration
    from app.db.models.identity import Student

    with session_factory() as session:
        student = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one()
        registrations = session.execute(
            select(EventRegistration).where(EventRegistration.event_id == 10, EventRegistration.student_id == student.id)
        ).scalars().all()
        assert len(registrations) == 1, "repeated resumption must never create a duplicate registration"
