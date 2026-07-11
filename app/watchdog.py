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

Persistent "done" marker (optional)
-----------------------------------
By default DONE lives only in memory, so a container restart/redeploy forgets it
and re-runs the day. Injecting a `marker` store makes "done" durable: after a
successful run the watchdog writes a marker (e.g. a blob), and every tick checks
the marker to decide whether the day is settled. This has two effects:

  * a restart/redeploy no longer re-runs an already-completed day, and
  * deleting or renaming the marker forces a re-run on the next tick, even on a
    long-lived container -- the manual override.

The marker is checked every tick (so the override works live) and is treated as
FAIL-OPEN: if the marker cannot be read, the day is treated as NOT done and the
job runs. A spurious re-run just overwrites the same partition, so the cost of
failing open is harmless; the cost of failing closed would be silently skipping
a day, which is not.

With no marker injected (marker=None, e.g. SINK=local / local dev) behaviour is
exactly as before: in-memory state only.

Dependencies (clock, awake-check, job, marker) are injected so this is
unit-testable without a database, real time, or blob storage.
"""
from __future__ import annotations

import enum
import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Optional, Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger("watchdog")


class State(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    MISSED = "missed"


class MarkerStore(Protocol):
    """Durable record of which watchdog days have completed.

    Implementations must be safe to call once per minute. `is_done` should
    fail-open internally where practical, but the watchdog also guards the call,
    so raising is tolerated (treated as not-done).
    """

    def is_done(self, logical_day: date) -> bool: ...
    def mark_done(self, logical_day: date, meta: dict | None = None) -> None: ...


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
        marker: Optional[MarkerStore] = None,
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
        self._marker = marker
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

    # --- marker helpers: always fail-open, never let the marker crash a tick ---

    def _marker_is_done(self, logical_day: date) -> bool:
        """True only if the marker positively reports done. Any error -> False
        (fail-open: run rather than silently skip)."""
        try:
            return bool(self._marker.is_done(logical_day))  # type: ignore[union-attr]
        except Exception:
            log.warning("Marker check failed for %s; failing open (will run).",
                        logical_day, exc_info=True)
            return False

    def _marker_mark_done(self, logical_day: date) -> None:
        """Persist 'done' for the day. Non-fatal on failure: the run already
        succeeded; a missing marker only means a restart may re-run (harmless)."""
        if self._marker is None:
            return
        try:
            self._marker.mark_done(logical_day, {"run_at": self._clock().isoformat()})
        except Exception:
            log.warning("Failed to write done-marker for %s (non-fatal).",
                        logical_day, exc_info=True)

    def tick(self) -> State:
        now = self._clock()
        day_start_dt = self._day_start_dt(now)
        logical_date = day_start_dt.date()   # identifies the watchdog day by its start date

        # New watchdog day: re-arm.
        if logical_date != self._run_date:
            self._run_date = logical_date
            self.state = State.PENDING
            log.info("Armed for watchdog day starting %s.", day_start_dt.isoformat())

        # Persistent marker is authoritative for DONE when present. Checked every
        # tick so that deleting/renaming the marker forces a re-run even on a
        # long-lived container (the manual override).
        if self._marker is not None:
            if self._marker_is_done(logical_date):
                if self.state is not State.DONE:
                    self.state = State.DONE
                    log.info("Marker present for %s; settled (done).", logical_date)
                return self.state
            # Marker absent -> not done. If we thought we were DONE, the marker
            # was cleared manually -> re-arm for a forced run.
            if self.state is State.DONE:
                log.info("Marker for %s cleared; re-arming for a forced run.",
                         logical_date)
                self.state = State.PENDING

        # Already settled for today (in-memory: local mode, and MISSED in any mode).
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
            self._marker_mark_done(self._run_date)
            log.info("Job done for watchdog day %s.", self._run_date)
        except Exception:
            self.state = State.PENDING
            log.exception("Job failed for %s; will retry next tick.", self._run_date)
        return self.state
