"""Build (or rebuild) a clean, isolated demonstration environment.

Creates a fresh SQLite database and Chroma policy store under ``data/demo/``,
seeds the fictional campus, and ingests the policy corpus. It never reads or
writes the development database (``data/campusnexus.db``) or the development
vector store (``data/chroma``). A reset deletes only the demo database and
demo policy store (other files in ``data/demo/``, such as saved live-LLM
reports, are kept), and it refuses to delete anything outside
``data/demo/``.

Why a rebuild (not just re-running seed_data.py) before a demo: seed data is
authored relative to the moment it is seeded (upcoming events, exam dates,
SLA deadlines), and seeding is idempotent -- so an old database keeps its old
dates, and past demo runs leave behind registrations, calendar entries,
cases and approvals. A rebuild restores every scenario to its intended
starting state.

Stop the API/Streamlit processes before running this (on Windows an open
SQLite/Chroma file cannot be deleted).

Usage:
    python scripts/reset_demo_env.py
    python scripts/reset_demo_env.py --embedding deterministic   # no model download / offline machine
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.auth.accounts import DEV_ACCOUNTS, resolve_seed_password, seed_dev_accounts
from app.db.session import create_db_engine, create_session_factory, init_db
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.ingest import ingest_policy_directory
from app.rag.vector_store import PolicyVectorStore
from scripts.seed_data import build_summary, run_seed

DEMO_DIR = REPO_ROOT / "data" / "demo"
DEMO_DB_PATH = DEMO_DIR / "campusnexus_demo.db"
DEMO_CHROMA_PATH = DEMO_DIR / "chroma"
# Kept across resets (only the DB and policy store are rebuilt); git-ignored with data/demo/.
DEMO_CREDENTIALS_PATH = DEMO_DIR / "dev_credentials.txt"


def _demo_state_paths() -> list[Path]:
    """Exactly what a reset rebuilds: the demo database (plus SQLite side files)
    and the demo policy store. Anything else in data/demo/ -- e.g. saved live-LLM
    reports -- is kept."""
    db = DEMO_DB_PATH
    return [db, *(db.with_name(db.name + suffix) for suffix in ("-wal", "-shm", "-journal")), DEMO_CHROMA_PATH]


def _remove_demo_dir() -> None:
    resolved = DEMO_DIR.resolve()
    if resolved.parent != (REPO_ROOT / "data").resolve() or resolved.name != "demo":
        raise SystemExit(f"Refusing to delete unexpected path: {resolved}")
    for path in _demo_state_paths():
        if path.resolve().parent != resolved:
            raise SystemExit(f"Refusing to delete unexpected path: {path.resolve()}")
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except PermissionError as exc:
            raise SystemExit(
                f"Could not delete {path} ({exc}). Stop the API/Streamlit processes using the demo "
                "environment, then re-run this script."
            ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild the isolated CampusNexus demo environment under data/demo/.")
    parser.add_argument(
        "--embedding",
        default=None,
        help="Embedding provider for the demo policy store: onnx_minilm (default) or deterministic. "
        "The API must be started with the same CAMPUSNEXUS_EMBEDDING_PROVIDER value.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _remove_demo_dir()
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    engine = create_db_engine(db_path=DEMO_DB_PATH)
    init_db(engine)
    session_factory = create_session_factory(engine)
    password, password_source = resolve_seed_password(DEMO_CREDENTIALS_PATH)
    with session_factory() as session:
        run_seed(session)
        seed_dev_accounts(session, password)
        summary = build_summary(session)
    engine.dispose()

    config = get_rag_config(chroma_path=DEMO_CHROMA_PATH, embedding_provider=args.embedding)
    embedding_provider = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    vector_store = PolicyVectorStore(
        path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding_provider
    )
    ingest = ingest_policy_directory(config.policy_dir, vector_store=vector_store)
    chunk_count = vector_store.count()

    if summary.students == 0 or chunk_count == 0:
        raise SystemExit(f"Demo environment is incomplete: students={summary.students}, policy chunks={chunk_count}.")

    print(f"Demo database:     {DEMO_DB_PATH}  ({summary.students} students, {summary.events} events)")
    print(f"Demo policy store: {DEMO_CHROMA_PATH}  ({ingest.document_count} documents, {chunk_count} chunks, "
          f"embedding={embedding_provider.name})")
    print("\nPoint the API at it (same terminal as uvicorn):")
    print("  PowerShell:")
    print('    $env:CAMPUSNEXUS_DB_PATH = "data/demo/campusnexus_demo.db"')
    print('    $env:CAMPUSNEXUS_VECTOR_STORE_PATH = "data/demo/chroma"')
    print(f'    $env:CAMPUSNEXUS_EMBEDDING_PROVIDER = "{config.embedding_provider}"')
    print("  bash:")
    print("    export CAMPUSNEXUS_DB_PATH=data/demo/campusnexus_demo.db")
    print("    export CAMPUSNEXUS_VECTOR_STORE_PATH=data/demo/chroma")
    print(f"    export CAMPUSNEXUS_EMBEDDING_PROVIDER={config.embedding_provider}")
    print("\nThen: python scripts/demo_preflight.py")
    print("\nReact app sign-in (development accounts, password from " + password_source + "):")
    for spec in DEV_ACCOUNTS:
        print(f"  {spec.email:<28} {spec.role.value}")


if __name__ == "__main__":
    main()
