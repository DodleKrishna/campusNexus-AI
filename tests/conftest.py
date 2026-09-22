"""Shared fixtures for the database/context-service/seed-data/RAG test suite.

Every test gets its own throwaway SQLite file under pytest's ``tmp_path`` and
its own engine/session, and ``CAMPUSNEXUS_DB_PATH`` is monkeypatched to that
path -- the development database under data/campusnexus.db is never opened by
the test suite. The RAG fixtures below are equally isolated: they build a
throwaway Chroma collection under pytest's session tmp dir using the
dependency-free DeterministicHashEmbedding, so the test suite never touches
the dev vector store at data/chroma and never hits the network.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import create_db_engine, create_session_factory, init_db
from scripts.seed_data import run_seed


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    db_file = tmp_path / "test_campusnexus.db"
    monkeypatch.setenv("CAMPUSNEXUS_DB_PATH", str(db_file))
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
