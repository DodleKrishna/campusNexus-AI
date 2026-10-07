"""Environment parsing for the Guardians' follow-up policies (AgentOS V2 Phase 4). Invalid values raise ``ValueError``
at startup; nothing here is decided by a model."""
from __future__ import annotations

import os
from datetime import time
from typing import Tuple

QUIET_HOURS_ENV = "CAMPUSNEXUS_FOLLOWUP_QUIET_HOURS"  # "21:00-07:00" campus time; "off" disables (exams, attendance)


def number(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    return float(raw) if raw else default


def hours_list(name: str, default: str) -> Tuple[float, ...]:
    return tuple(float(h) for h in (os.environ.get(name) or default).split(",") if h.strip())


def quiet_hours(name: str = QUIET_HOURS_ENV, default: str = "21:00-07:00") -> Tuple[time, time]:
    value = (os.environ.get(name) or default).strip().lower()
    if value == "off":
        return time(0, 0), time(0, 0)
    start, end = value.split("-")
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())
