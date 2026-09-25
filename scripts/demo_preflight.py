"""Pre-demo readiness check. Run it in the same shell (same env vars) you will
start the API from.

Checks, in order: database reachable + seeded + demo student present + seed
dates still in the future; policy store non-empty and actually returning
evidence; LLM provider constructible in the configured mode (and, with
``--live-call``, one real structured call); and whether the API is already
running and reports the same mode.

Exit code 0 = ready, 1 = something must be fixed first. Never substitutes the
mock provider for a misconfigured real one -- it reports the failure.

Usage:
    python scripts/demo_preflight.py
    python scripts/demo_preflight.py --live-call     # also spend one small real LLM request
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import func, select

from app.db.models import Event, Student
from app.db.session import create_session_factory, describe_database, get_database_url, open_database, safe_error
from app.llm.base import LLMProviderError
from app.llm.factory import get_llm_provider
from app.rag.config import get_rag_config
from app.rag.embeddings import get_embedding_provider
from app.rag.retriever import PolicyRetriever
from app.rag.vector_store import PolicyVectorStore
from app.services.knowledge import KnowledgeService

DEMO_STUDENT = "STU-DEMO-001"

Result = Tuple[str, bool, str]


def check_database() -> List[Result]:
    results: List[Result] = []
    url = get_database_url()
    if describe_database(url)["dialect"] == "sqlite":
        db_path = url.removeprefix("sqlite:///")
        if not Path(db_path).exists():
            return [("database", False, f"{db_path} does not exist. Run: python scripts/reset_demo_env.py")]
        label = db_path
    else:
        # Phase 21: never print a PostgreSQL URL, host or user -- only which kind of database it is.
        label = describe_database(url)["label"]
    try:
        engine = open_database()
    except Exception as exc:  # noqa: BLE001 -- a preflight reports, never crashes
        return [("database", False, f"{label}: {safe_error(exc, url)}")]
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            students = session.execute(select(func.count()).select_from(Student)).scalar_one()
            demo = session.execute(select(Student).where(Student.student_code == DEMO_STUDENT)).scalar_one_or_none()
            upcoming = session.execute(
                select(func.count()).select_from(Event).where(Event.start_at > datetime.now(timezone.utc))
            ).scalar_one()
    except Exception as exc:  # noqa: BLE001 -- a preflight reports, never crashes
        return [("database", False, f"{label}: {safe_error(exc, url)}")]
    finally:
        engine.dispose()
    results.append(("database seeded", students > 0, f"{label} ({students} students)"))
    results.append(("demo student", demo is not None, DEMO_STUDENT))
    results.append((
        "upcoming events",
        upcoming > 0,
        f"{upcoming} events still in the future" + ("" if upcoming else ". Seed dates are stale: re-run reset_demo_env.py (SQLite) or seed_database.py --demo-classes (PostgreSQL)"),
    ))
    return results


def check_policy_store() -> List[Result]:
    config = get_rag_config()
    try:
        embedding = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
        store = PolicyVectorStore(path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding)
        chunks = store.count()
        evidence = KnowledgeService(retriever=PolicyRetriever(vector_store=store, embedding_provider=embedding)).search(
            "minimum attendance requirement for exam eligibility", top_k=3
        )
    except Exception as exc:  # noqa: BLE001
        return [("policy store", False, f"{config.chroma_path}: {type(exc).__name__}: {exc}")]
    return [
        ("policy store", chunks > 0, f"{config.chroma_path} ({chunks} chunks, embedding={embedding.name})"),
        ("evidence retrieval", bool(evidence), f"{len(evidence)} citation(s) for a sample attendance query"),
    ]


def check_llm(live_call: bool) -> List[Result]:
    configured = os.environ.get("CAMPUSNEXUS_LLM_PROVIDER") or "mock"
    try:
        provider = get_llm_provider(None)
    except (LLMProviderError, ValueError) as exc:
        return [("llm provider", False, f"CAMPUSNEXUS_LLM_PROVIDER={configured}: {exc}")]
    if not provider.is_live:
        return [("llm provider", True, "mock (offline, deterministic). No AI model will be called")]
    results: List[Result] = [("llm provider", True, f"{provider.name} (live, model={provider.model_name})")]
    if live_call:
        try:
            intent = provider.classify_events_intent("Find workshops about machine learning")
            results.append(("live llm call", True, f"structured intent returned: {intent.intent.value}"))
        except LLMProviderError as exc:
            results.append(("live llm call", False, str(exc)))
    return results


def check_api() -> List[Result]:
    import httpx

    url = os.environ.get("CAMPUSNEXUS_API_URL") or "http://127.0.0.1:8000"
    try:
        health = httpx.get(f"{url}/health", timeout=5).json()
    except Exception:  # noqa: BLE001
        return [("api", True, f"not running at {url} (start it after this check passes)")]
    llm = health.get("llm") or {}
    ok = bool(health.get("ready"))
    return [(
        "api",
        ok,
        f"running at {url}: ready={health.get('ready')}, llm={llm.get('provider')} live={llm.get('live')}"
        + ("" if ok else ". The running API is not ready (see database/policy_store in /health)"),
    )]


def main() -> None:
    parser = argparse.ArgumentParser(description="CampusNexus pre-demo readiness check")
    parser.add_argument("--live-call", action="store_true", help="Make one small real LLM request (live mode only).")
    args = parser.parse_args()

    results = check_database() + check_policy_store() + check_llm(args.live_call) + check_api()
    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    failed = [name for name, ok, _ in results if not ok]
    print("\nREADY" if not failed else f"\nNOT READY: fix {', '.join(failed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
