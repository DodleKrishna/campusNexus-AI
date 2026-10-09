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
    python scripts/process_due_missions.py --loop --interval 15   # one bounded batch every 15 s until Ctrl+C

Loop mode (for running deployments; there is no in-process scheduler in the API): one bounded batch at a time, then
a sleep. Ctrl+C (SIGINT) or SIGTERM never interrupts a batch: the current batch finishes and commits, then the
process exits 0. Crash/restart safety comes from the database, not this process: missions and communication jobs are
claimed with leases (a crashed worker's lease simply expires), follow-ups are unique per (source, student) and jobs
per follow-up (idempotency key), so a restart never sends a reminder twice. A failing batch is reported (error class
only) and retried on the next tick.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
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

MIN_INTERVAL = 5


class StopFlag:
    """Set by SIGINT/SIGTERM; checked only between batches, so a batch is never cut in half."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self, *_: object) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds`` in short slices (a Windows console only delivers Ctrl+C between them)."""
        deadline = time.monotonic() + seconds
        while not self._event.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self._event.wait(min(left, 0.5))
        return self._event.is_set()


def run_batch(factory, runtime, recorder, *, limit: int, max_transitions: int) -> dict:
    scan = scan_attendance(factory, runtime)
    communication = process_communication_jobs(factory, runtime)
    report = process_due_missions(factory, runtime, limit=limit, max_transitions=max_transitions, recorder=recorder)
    return {"attendance_scan": scan.model_dump(mode="json"), "communication": communication.model_dump(mode="json"),
            **report.model_dump(mode="json")}


def run_loop(batch, *, interval: float, stop: StopFlag, once: bool = False, max_batches: int | None = None) -> int:
    """Run ``batch()`` (one bounded unit of work), then wait ``interval`` seconds, until ``stop`` is set."""
    done = 0
    while not stop.is_set():
        try:
            print(json.dumps(batch(), separators=(",", ":")), flush=True)
        except Exception as exc:  # noqa: BLE001 -- report the class only (messages may contain data); retry next tick
            print(json.dumps({"error": "BATCH_FAILED", "type": type(exc).__name__}), flush=True)
        done += 1
        if once or (max_batches is not None and done >= max_batches):
            break
        stop.wait(interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process one batch and exit")
    mode.add_argument("--loop", action="store_true", help="process a batch every --interval seconds until Ctrl+C "
                      "(the default when --once is not given)")
    parser.add_argument("--interval", type=float, default=60, help=f"seconds between batches (>= {MIN_INTERVAL})")
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
    stop = StopFlag()
    signal.signal(signal.SIGINT, stop.set)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop.set)
    code = run_loop(lambda: run_batch(factory, runtime, recorder, limit=args.limit, max_transitions=args.max_transitions),
                    interval=args.interval, stop=stop, once=args.once)
    if not args.once:
        print(json.dumps({"stopped": True}), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
