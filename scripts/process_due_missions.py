"""Advance due autonomous AgentOS missions (the Guardians and the Communication Agent).

Each batch first runs the deterministic absence-detection pass (``scan_attendance``: due class-attendance
events -> interventions + Attendance Guardian missions; no AI), then the communication worker
(``process_communication_jobs``: follow-up requests -> jobs + Communication Agent missions, and delivery of due
jobs through their connectors; no AI), then
``app.agentos.worker.process_due_missions`` against the configured database
(``CAMPUSNEXUS_DATABASE_URL`` / ``CAMPUSNEXUS_DB_PATH``, as every other script) with the
configured brain (``CAMPUSNEXUS_AGENT_BRAIN``, default the offline mock; Phase 6:
``CAMPUSNEXUS_INTELLIGENCE_MODE=cloud|local|auto`` routes through the local Ollama brain when offline, and the
communication worker defers internet-only channels while the deployment is offline). Bounded per run:
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

from app.agentos.attendance_guardian import scan_attendance  # noqa: E402
from app.agentos.bootstrap import build_agent_runtime  # noqa: E402
from app.agentos.brain_router import build_configured_brain  # noqa: E402
from app.agentos.connectivity import connectivity_from_env  # noqa: E402
from app.communication.worker import process_communication_jobs  # noqa: E402
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
    connectivity = connectivity_from_env()
    brain = build_configured_brain(recorder=recorder, connectivity=connectivity)
    if not getattr(brain, "available", True):
        print(json.dumps({"error": getattr(brain, "code", "BRAIN_UNAVAILABLE")}))
        return 2
    runtime = build_agent_runtime(brain, connectivity=connectivity)
    while True:
        scan = scan_attendance(factory, runtime)
        communication = process_communication_jobs(factory, runtime)
        report = process_due_missions(factory, runtime, limit=args.limit, max_transitions=args.max_transitions,
                                      recorder=recorder)
        print(json.dumps({"attendance_scan": scan.model_dump(mode="json"),
                          "communication": communication.model_dump(mode="json"), **report.model_dump(mode="json")},
                         separators=(",", ":")), flush=True)
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
