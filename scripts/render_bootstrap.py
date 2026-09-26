"""One-time demo initialization for an ephemeral hosted deployment (e.g. Render free tier).

Enabled only when ``CAMPUSNEXUS_DEMO_BOOTSTRAP=1``: the API calls ``bootstrap_demo`` once at
startup (app lifespan), never per request. It is idempotent and never deletes anything:

* SQLite database has no students -> create the schema and load the deterministic demo seed
  (the ``campusnexus-demo`` organization, its six agent deployments, the development accounts
  and the Phase 16/17 extra classes) -- the same steps as ``scripts/reset_demo_env.py``.
* Policy store is empty -> ingest ``data/policies`` (use CAMPUSNEXUS_EMBEDDING_PROVIDER=deterministic
  on hosts without the ONNX model).

Refuses to run against PostgreSQL (``CAMPUSNEXUS_DATABASE_URL``): remote data is never seeded
from here. The account password comes only from ``CAMPUSNEXUS_DEMO_PASSWORD`` and is never logged.

Can also be run directly: ``python scripts/render_bootstrap.py``.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import Engine, func, select  # noqa: E402

logger = logging.getLogger("campusnexus.bootstrap")

BOOTSTRAP_ENV = "CAMPUSNEXUS_DEMO_BOOTSTRAP"


def enabled() -> bool:
    return os.environ.get(BOOTSTRAP_ENV, "").strip() == "1"


def bootstrap_demo(engine: Engine, vector_store: Any, policy_dir: Path) -> dict:
    """Seed an empty SQLite demo database and an empty policy store. Returns what it did (no secrets)."""
    from app.auth.accounts import seed_dev_accounts
    from app.db.models.identity import Student
    from app.db.session import create_session_factory, init_db
    from app.rag.ingest import ingest_policy_directory
    from scripts.schedule_demo_class import schedule_extra_class, schedule_tomorrow_afternoon
    from scripts.seed_data import run_seed

    if engine.dialect.name != "sqlite":
        raise RuntimeError(f"{BOOTSTRAP_ENV}=1 only seeds a local SQLite demo database; refusing {engine.dialect.name}.")
    database = engine.url.database
    if database:
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    report = {"seeded": False, "ingested": False}
    init_db(engine)  # create missing tables / additive upgrade only
    with create_session_factory(engine)() as session:
        students = session.execute(select(func.count()).select_from(Student)).scalar_one()
        if students == 0:
            password = os.environ.get("CAMPUSNEXUS_DEMO_PASSWORD", "").strip()
            if not password:
                raise RuntimeError("CAMPUSNEXUS_DEMO_PASSWORD must be set to seed the demo accounts.")
            run_seed(session)
            seed_dev_accounts(session, password)
            try:
                schedule_extra_class(session)
                schedule_tomorrow_afternoon(session)
            except Exception:  # noqa: BLE001 -- optional demo classes; the demo works without them
                logger.exception("could not schedule the extra demo classes")
            report["seeded"] = True
        report["students"] = session.execute(select(func.count()).select_from(Student)).scalar_one()
    if vector_store.count() == 0:
        ingest_policy_directory(policy_dir, vector_store=vector_store)
        report["ingested"] = True
    report["policy_chunks"] = vector_store.count()
    logger.warning("demo bootstrap: %s", report)
    return report


def main() -> int:
    from app.db.session import create_db_engine
    from app.rag.config import get_rag_config
    from app.rag.embeddings import get_embedding_provider
    from app.rag.vector_store import PolicyVectorStore

    config = get_rag_config()
    embedding = get_embedding_provider(config.embedding_provider, model_name=config.embedding_model)
    store = PolicyVectorStore(path=config.chroma_path, collection_name=config.collection_name, embedding_provider=embedding)
    engine = create_db_engine()
    try:
        print(bootstrap_demo(engine, store, config.policy_dir))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
