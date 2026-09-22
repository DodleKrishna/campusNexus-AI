"""RAG configuration: vector store path, policy corpus path, embedding choice.

Mirrors the resolution pattern in app/db/session.py -- an explicit argument
wins, then the environment variable, then a repo-relative default. No secrets
are needed for the local embedding providers this phase ships.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CHROMA_PATH = BASE_DIR / "data" / "chroma"
DEFAULT_POLICY_DIR = BASE_DIR / "data" / "policies"
DEFAULT_COLLECTION_NAME = "campusnexus_policies"

# "onnx_minilm": local ONNX MiniLM (all-MiniLM-L6-v2) via chromadb's bundled
# embedding function -- the production/demo default (see app/rag/embeddings.py).
# "deterministic": offline hashing-trick embedding with no model download --
# always used by the test suite, and available as a safe fallback.
DEFAULT_EMBEDDING_PROVIDER = "onnx_minilm"
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


@dataclass(frozen=True)
class RAGConfig:
    chroma_path: Path
    policy_dir: Path
    collection_name: str
    embedding_provider: str
    embedding_model: str


def _resolve_path(explicit: Optional[str | Path], env_var: str, default: Path) -> Path:
    raw = explicit if explicit is not None else os.environ.get(env_var)
    if raw is None:
        return default
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def get_rag_config(
    *,
    chroma_path: Optional[str | Path] = None,
    policy_dir: Optional[str | Path] = None,
    collection_name: Optional[str] = None,
    embedding_provider: Optional[str] = None,
    embedding_model: Optional[str] = None,
) -> RAGConfig:
    """Resolve RAG configuration from explicit args, then environment, then defaults."""
    return RAGConfig(
        chroma_path=_resolve_path(chroma_path, "CAMPUSNEXUS_VECTOR_STORE_PATH", DEFAULT_CHROMA_PATH),
        policy_dir=_resolve_path(policy_dir, "CAMPUSNEXUS_POLICY_DIR", DEFAULT_POLICY_DIR),
        collection_name=collection_name or os.environ.get("CAMPUSNEXUS_CHROMA_COLLECTION") or DEFAULT_COLLECTION_NAME,
        embedding_provider=(
            embedding_provider or os.environ.get("CAMPUSNEXUS_EMBEDDING_PROVIDER") or DEFAULT_EMBEDDING_PROVIDER
        ),
        embedding_model=(
            embedding_model or os.environ.get("CAMPUSNEXUS_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
        ),
    )
