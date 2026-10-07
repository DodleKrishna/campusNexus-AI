"""Advance due autonomous AgentOS missions (AgentOS V2 Phase 3: the Assignment Guardian).

Runs ``app.agentos.worker.process_due_missions`` against the configured database
(``CAMPUSNEXUS_DATABASE_URL`` / ``CAMPUSNEXUS_DB_PATH``, as every other script) with the
configured brain (``CAMPUSNEXUS_AGENT_BRAIN``, default the offline mock). Bounded per run:
``--limit`` missions, ``--max-transitions`` each. Prints one JSON report per run (ids,
statuses, codes; no prompts or personal data).

    python scripts/process_due_missions.py --once
    python scripts/process_due_missions.py --interval 60      # repeat until Ctrl+C
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agentos.bootstrap import build_agent_runtime  # noqa: E402
from app.agentos.providers import build_agent_brain  # noqa: E402
from app.agentos.worker import DEFAULT_LIMIT, DEFAULT_TRANSITIONS, MAX_BATCH, process_due_missions  # noqa: E402
from app.db.session import open_database  # noqa: E402
from app.db.tenant_session import TenantSessionFactory  # noqa: E402
from app.services.ai_usage import AIUsageRecorder  # noqa: E402

MIN_INTERVAL = 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument("--interval", type=int, default=60, help=f"seconds between batches (>= {MIN_INTERVAL})")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"missions per batch (1-{MAX_BATCH})")
    parser.add_argument("--max-transitions", type=int, default=DEFAULT_TRANSITIONS, help="transitions per mission")
    args = parser.parse_args(argv)
    if args.interval < MIN_INTERVAL:
        parser.error(f"--interval must be at least {MIN_INTERVAL} seconds")

    factory = TenantSessionFactory(open_database())
    recorder = AIUsageRecorder(factory)
    brain = build_agent_brain(recorder=recorder)
    if not getattr(brain, "available", True):
        print(json.dumps({"error": getattr(brain, "code", "BRAIN_UNAVAILABLE")}))
        return 2
    runtime = build_agent_runtime(brain)
    while True:
        report = process_due_missions(factory, runtime, limit=args.limit, max_transitions=args.max_transitions,
                                      recorder=recorder)
        print(json.dumps(report.model_dump(mode="json"), separators=(",", ":")), flush=True)
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
