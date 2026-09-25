"""Shared fixtures for the database/context-service/seed-data/RAG test suite.

Every test gets its own throwaway SQLite file under pytest's ``tmp_path`` and
its own engine/session, and ``CAMPUSNEXUS_DB_PATH`` is monkeypatched to that
path -- the development database under data/campusnexus.db is never opened by
the test suite. The RAG fixtures below are equally isolated: they build a
throwaway Chroma collection under pytest's session tmp dir using the
dependency-free DeterministicHashEmbedding, so the test suite never touches
the dev vector store at data/chroma and never hits the network.

Phase 21: ``CAMPUSNEXUS_DATABASE_URL`` is removed from the environment before
any test runs (it would otherwise outrank ``CAMPUSNEXUS_DB_PATH`` and send
code paths -- and subprocesses -- to a real, possibly remote, database).
PostgreSQL is used only through ``CAMPUSNEXUS_TEST_DATABASE_URL`` (see
tests/pg_support.py).
"""
from __future__ import annotations

import os

os.environ.pop("CAMPUSNEXUS_DATABASE_URL", None)

import pytest  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.session import create_db_engine, create_session_factory, init_db  # noqa: E402
from scripts.seed_data import run_seed  # noqa: E402
from tests.pg_support import all_on_postgres, postgres_schema_engine  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Mark the focused real-Supabase subset (tests/test_phase21_postgres.py SUPABASE_ACCEPTANCE).

    Marked per collected item, not on the function: several of those tests are imported from other
    modules, and their original SQLite runs must stay unmarked.
    """
    for item in items:
        module = getattr(item, "module", None)
        if module is not None and item.originalname in getattr(module, "SUPABASE_ACCEPTANCE", ()):
            item.add_marker(pytest.mark.supabase_acceptance)


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    db_file = tmp_path / "test_campusnexus.db"
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(db_file))
    if all_on_postgres():
        with postgres_schema_engine() as eng:
            init_db(eng)
            yield eng
        return
    eng = create_db_engine(db_path=str(db_file))
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture()
def session(session_factory: sessionmaker[Session]):
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def seeded_session(session_factory: sessionmaker[Session]):
    """A session against a freshly-seeded (once) test database."""
    with session_factory() as setup_session:
        run_seed(setup_session)

    s = session_factory()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# RAG fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def policy_dir():
    from app.rag.config import DEFAULT_POLICY_DIR

    return DEFAULT_POLICY_DIR


@pytest.fixture(scope="session")
def deterministic_embedding_provider():
    from app.rag.embeddings import DeterministicHashEmbedding

    return DeterministicHashEmbedding()


@pytest.fixture(scope="session")
def rag_vector_store(tmp_path_factory, deterministic_embedding_provider, policy_dir):
    """A Chroma collection, freshly ingested once per test session, in a temp dir."""
    from app.rag.ingest import ingest_policy_directory
    from app.rag.vector_store import PolicyVectorStore

    chroma_dir = tmp_path_factory.mktemp("chroma_test_store")
    store = PolicyVectorStore(
        path=chroma_dir, collection_name="test_policies", embedding_provider=deterministic_embedding_provider
    )
    ingest_policy_directory(policy_dir, vector_store=store)
    return store


@pytest.fixture(scope="session")
def rag_retriever(rag_vector_store, deterministic_embedding_provider):
    from app.rag.retriever import PolicyRetriever

    return PolicyRetriever(vector_store=rag_vector_store, embedding_provider=deterministic_embedding_provider)


@pytest.fixture(scope="session")
def knowledge_service(rag_retriever):
    from app.services.knowledge import KnowledgeService

    return KnowledgeService(retriever=rag_retriever)


# ---------------------------------------------------------------------------
# Phase 8 API fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_app(seeded_session, session_factory, knowledge_service):
    """A fully-wired FastAPI app (app.api.main.create_app) against the same
    isolated, freshly-seeded temp DB every other test uses -- never the
    shared dev DB. ``seeded_session`` is requested only for its seeding side
    effect (mirrors tests/test_multiagent_orchestration.py's convention)."""
    from app.api.main import create_app
    from app.llm.providers.mock import MockLLMProvider
    from app.tools.build import build_default_tool_registry

    return create_app(
        session_factory=session_factory,
        knowledge_service=knowledge_service,
        llm_provider=MockLLMProvider(),
        tool_gateway=build_default_tool_registry(),
    )


@pytest.fixture()
def api_client(api_app):
    from fastapi.testclient import TestClient

    with TestClient(api_app) as client:
        yield client
