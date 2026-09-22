"""Ingest the policy document corpus (data/policies/) into the Chroma vector store.

Idempotent: re-running this script on an unchanged corpus upserts the same
chunk_ids in place rather than duplicating rows (see app/rag/vector_store.py).

Usage:
    python scripts/ingest_policies.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.ingest import ingest_policy_directory
from app.rag.vector_store import PolicyVectorStore


def main() -> None:
    config = get_rag_config()
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path,
        collection_name=config.collection_name,
        embedding_provider=embedding_provider,
    )

    print(f"Embedding provider: {embedding_provider.name}")
    print(f"Policy directory:   {config.policy_dir}")
    print(f"Chroma path:        {config.chroma_path}")

    summary = ingest_policy_directory(config.policy_dir, vector_store=vector_store)

    print(f"Ingested {summary.document_count} policy documents, {summary.chunk_count} chunks.")
    print(f"Collection '{config.collection_name}' now has {vector_store.count()} chunks total.")


if __name__ == "__main__":
    main()
