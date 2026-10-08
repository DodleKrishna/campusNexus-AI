"""Index the CAMPUS AI demo institution documents into the Chroma store (deterministic, idempotent).

Reads ``data/campus_ai_knowledge/*.md``, chunks them with the existing RAG pipeline and upserts them into the
``campus_ai_knowledge`` collection of the configured vector store (``CAMPUSNEXUS_VECTOR_STORE_PATH``, or
``--chroma``), tagged with the demo organization's id. Re-running replaces the same chunk ids in place.

The organization id is read from a local SQLite database (default: the demo database); this script never opens
``CAMPUSNEXUS_DATABASE_URL``.

Usage:
    python scripts/index_campus_knowledge.py
    python scripts/index_campus_knowledge.py --chroma data/demo/chroma --embedding deterministic
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models.organization import Organization  # noqa: E402
from app.db.tenancy import DEFAULT_ORGANIZATION_SLUG  # noqa: E402
from app.rag.campus_knowledge import CAMPUS_KNOWLEDGE_DIR, KnowledgeIndexSummary, index_campus_knowledge, open_campus_store  # noqa: E402
from app.rag.config import get_rag_config  # noqa: E402
from app.rag.embeddings import get_embedding_provider  # noqa: E402

DEMO_DB_PATH = REPO_ROOT / "data" / "demo" / "campusnexus_demo.db"


def organization_id_from_sqlite(db_path: Path, slug: str = DEFAULT_ORGANIZATION_SLUG) -> int:
    from app.db.session import create_session_factory, open_database

    engine = open_database(str(db_path))  # an explicit path is always SQLite
    try:
        with create_session_factory(engine)() as session:
            organization_id = session.execute(select(Organization.id).where(Organization.slug == slug)).scalar_one_or_none()
    finally:
        engine.dispose()
    if organization_id is None:
        raise SystemExit(f"Organization {slug!r} not found in {db_path}. Seed the database first.")
    return organization_id


def index(*, organization_id: int, chroma_path: Path | None = None, embedding: str | None = None,
          slug: str = DEFAULT_ORGANIZATION_SLUG) -> KnowledgeIndexSummary:
    config = get_rag_config(chroma_path=chroma_path, embedding_provider=embedding)
    provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    store = open_campus_store(chroma_path=config.chroma_path, embedding_provider=provider)
    return index_campus_knowledge(CAMPUS_KNOWLEDGE_DIR, vector_store=store, organization_id=organization_id,
                                  organization_slug=slug)


def main() -> None:
    parser = argparse.ArgumentParser(description="Index the CAMPUS AI demo institution documents.")
    parser.add_argument("--db", default=str(DEMO_DB_PATH), help="SQLite database holding the organization.")
    parser.add_argument("--chroma", default=None, help="Chroma path (default: CAMPUSNEXUS_VECTOR_STORE_PATH).")
    parser.add_argument("--embedding", default=None, help="onnx_minilm (default) or deterministic.")
    args = parser.parse_args()
    summary = index(organization_id=organization_id_from_sqlite(Path(args.db)),
                    chroma_path=Path(args.chroma) if args.chroma else None, embedding=args.embedding)
    print(f"CAMPUS AI knowledge indexed for organization {summary.organization_id}: "
          f"{summary.document_count} documents, {summary.chunk_count} chunks ({summary.skipped_documents} skipped).")


if __name__ == "__main__":
    main()
