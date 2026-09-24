"""Schedule an extra class meeting that is current right now (Phase 16 demo tooling).

The seeded timetable is recurring (Aditi's classes are at 10:00 on weekdays),
so whether a class is "currently relevant" depends on when the demo runs.
This script records one real extra class -- an ``attendance_sessions`` row with
no timetable slot, status SCHEDULED -- for a faculty member's teaching
assignment, starting at the current time rounded down to 5 minutes. It then
behaves exactly like any other class: the faculty member must start it, mark
attendance and close it through the normal API. Nothing is started or marked
here.

Run it against the demo database (``scripts/reset_demo_env.py`` already calls
it once); re-run it if the demo happens more than an hour after the reset.

Phase 17: ``--tomorrow-afternoon`` also records a real extra class tomorrow at
14:00 for the same teaching assignment, so a faculty-leave request for
"tomorrow afternoon" has an affected class to show the HOD.

Usage:
    python scripts/schedule_demo_class.py                       # CS303 by Dr. Ashok Verma, now, 60 min
    python scripts/schedule_demo_class.py --tomorrow-afternoon  # plus an extra class tomorrow 14:00-15:00
    python scripts/schedule_demo_class.py --course CS303 --minutes 90
    python scripts/schedule_demo_class.py --db data/demo/campusnexus_demo.db
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.academic import Course
from app.db.models.faculty import AttendanceSession, AttendanceSessionStatus, TeachingAssignment
from app.db.session import create_session_factory, open_database
from app.services.class_schedule import local

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "demo" / "campusnexus_demo.db"


def demo_window(now_local: datetime, minutes: int) -> tuple[datetime, datetime]:
    """A demo class window around ``now_local`` that never crosses midnight (Phase 18).

    Normally it starts at ``now`` rounded down to 5 minutes. If that window would
    run past the end of the day, it is moved earlier so it ends at 23:59 on the
    same date (still containing ``now`` unless the day is almost over). Demo
    tooling only -- the real timetable is never adjusted.
    """
    start = now_local.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 5)
    end = start + timedelta(minutes=minutes)
    day_end = start.replace(hour=23, minute=59)
    if end > day_end:
        end = day_end
        start = max(end - timedelta(minutes=minutes), start.replace(hour=0, minute=0))
    return start, end


def schedule_extra_class(
    session: Session, *, course_code: str = "CS303", section: str = "1", minutes: int = 60,
    now: Optional[datetime] = None, room: Optional[str] = None, start_at: Optional[datetime] = None,
) -> AttendanceSession:
    now = now or datetime.now(timezone.utc)
    assignment = session.execute(
        select(TeachingAssignment).join(Course, Course.id == TeachingAssignment.course_id)
        .where(Course.code == course_code, TeachingAssignment.section == section)
    ).scalar_one_or_none()
    if assignment is None:
        raise SystemExit(f"No teaching assignment for {course_code} section {section}. Seed the database first.")
    if start_at is not None:
        start_local = local(start_at)
        end_local = start_local + timedelta(minutes=minutes)
    else:
        start_local, end_local = demo_window(local(now), minutes)
    start = start_local.astimezone(timezone.utc)
    existing = session.execute(
        select(AttendanceSession).where(
            AttendanceSession.teaching_assignment_id == assignment.id, AttendanceSession.scheduled_start == start
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = AttendanceSession(
        teaching_assignment_id=assignment.id, course_id=assignment.course_id, faculty_id=assignment.faculty_id,
        timetable_slot_id=None, session_date=start_local.date(), scheduled_start=start,
        scheduled_end=end_local.astimezone(timezone.utc), room=room or "Block A - Room 206",
        status=AttendanceSessionStatus.SCHEDULED, note="Extra class",
    )
    session.add(row)
    session.commit()
    return row


def schedule_tomorrow_afternoon(session: Session, *, course_code: str = "CS303", now: Optional[datetime] = None) -> AttendanceSession:
    """An extra class tomorrow 14:00-15:00 campus time (Phase 17 faculty-leave demo)."""
    now = now or datetime.now(timezone.utc)
    tomorrow = local(now).date() + timedelta(days=1)
    start = datetime.combine(tomorrow, time(14, 0), tzinfo=local(now).tzinfo)
    return schedule_extra_class(session, course_code=course_code, minutes=60, now=now, start_at=start)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database (default: the demo database).")
    parser.add_argument("--course", default="CS303")
    parser.add_argument("--section", default="1")
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--tomorrow-afternoon", action="store_true", help="Also schedule an extra class tomorrow 14:00-15:00.")
    args = parser.parse_args()
    engine = open_database(args.db)
    with create_session_factory(engine)() as session:
        rows = [schedule_extra_class(session, course_code=args.course, section=args.section, minutes=args.minutes)]
        if args.tomorrow_afternoon:
            rows.append(schedule_tomorrow_afternoon(session, course_code=args.course))
        for row in rows:
            print(
                f"Extra class scheduled: {row.course.title} (section {args.section}) by {row.faculty.full_name}, "
                f"{local(row.scheduled_start):%a %d %b %H:%M}-{local(row.scheduled_end):%H:%M} IST, {row.room}. "
                f"Status: {row.status.value}."
            )
    engine.dispose()


if __name__ == "__main__":
    main()
