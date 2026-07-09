r"""
Daily watchdog: runs one job per "watchdog day", the first time the Pi becomes
reachable within that day.

A watchdog day begins at `day_start` (a wall-clock time in `tz`) and runs until
the next `day_start`. With day_start=16:00 and tz=UTC the day runs 16:00 UTC
through 15:59:59 UTC the following day, and the rollover (re-arm) happens at
16:00 UTC. Set day_start=00:00 (the default) for plain calendar-day behaviour.

Call tick() on a fixed cadence (every minute). The state machine:

    PENDING --awake--> RUNNING --success--> DONE
       |                         \--failure--> PENDING (retry next tick)
       |--past cutoff--> MISSED
    (crossing day_start re-arms to PENDING for the new watchdog day)

Once DONE or MISSED, ticks are no-ops until the next day_start. Dependencies
(clock, awake-check, job) are injected so this is unit-testable without a
database or real time.
"""
from __future__ import annotations

import enum
import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("watchdog")


class State(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    MISSED = "missed"


class DailyWatchdog:
    def __init__(
        self,
        *,
        job: Callable[[date], None],
        is_awake: Callable[[], bool],
        cutoff: Optional[dtime] = None,
        day_start: dtime = dtime(0, 0),
        tz: str = "UTC",
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._job = job
        self._is_awake = is_awake
        self._day_start = day_start
        # Default cutoff = the whole watchdog day: the minute before the next
        # day_start. This stays correct for any day_start (00:00 -> 23:59,
        # 16:00 -> 15:59), so the give-up time can't silently contradict the
        # day boundary. Pass an explicit cutoff only to give up *earlier*.
        if cutoff is None:
            anchor = datetime(2000, 1, 1)  # arbitrary safe date; avoids min/max overflow
            cutoff = (datetime.combine(anchor.date(), day_start)
                      - timedelta(minutes=1)).time()
        self._cutoff = cutoff
        self._tz = ZoneInfo(tz)
        self._clock = clock or (lambda: datetime.now(self._tz))
        self._run_date: date | None = None
        self.state = State.PENDING

    def _day_start_dt(self, now: datetime) -> datetime:
        """The instant at which the current watchdog day began (always <= now)."""
        start = datetime.combine(now.date(), self._day_start, tzinfo=self._tz)
        if now < start:               # before today's boundary -> day began yesterday
            start -= timedelta(days=1)
        return start

    def _cutoff_dt(self, day_start_dt: datetime) -> datetime:
        """The MISSED cutoff instant, resolved within the current watchdog day.

        The cutoff wall-time is anchored to the first occurrence at or after the
        day's start, so a cutoff that reads 'earlier' than day_start correctly
        lands on the following calendar day (e.g. day_start 16:00, cutoff 02:00).
        """
        c = datetime.combine(day_start_dt.date(), self._cutoff, tzinfo=self._tz)
        if c < day_start_dt:
            c += timedelta(days=1)
        return c

    def tick(self) -> State:
        now = self._clock()
        day_start_dt = self._day_start_dt(now)
        logical_date = day_start_dt.date()   # identifies the watchdog day by its start date

        # New watchdog day: re-arm.
        if logical_date != self._run_date:
            self._run_date = logical_date
            self.state = State.PENDING
            log.info("Armed for watchdog day starting %s.", day_start_dt.isoformat())

        # Already settled for today.
        if self.state in (State.DONE, State.MISSED):
            return self.state

        # Ran out of the day's window.
        if now >= self._cutoff_dt(day_start_dt):
            self.state = State.MISSED
            log.warning("Cutoff reached; no successful run for watchdog day %s.",
                        self._run_date)
            return self.state

        # Not reachable yet — try again next tick.
        if not self._is_awake():
            log.info("Pi not reachable; will retry.")
            return self.state

        # Reachable and pending — run once.
        self.state = State.RUNNING
        try:
            self._job(self._run_date)
            self.state = State.DONE
            log.info("Job done for watchdog day %s.", self._run_date)
        except Exception:
            self.state = State.PENDING
            log.exception("Job failed for %s; will retry next tick.", self._run_date)
        return self.state
