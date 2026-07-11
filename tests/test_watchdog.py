from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.watchdog import DailyWatchdog, State

TZ = "UTC"

# All fake clocks are offsets from this single anchor, so the suite is fully
# deterministic (never reads the real wall clock) and there are no scattered
# date literals to keep in sync.
BASE = datetime(2026, 1, 1, tzinfo=ZoneInfo(TZ))


def at(days=0, hours=0, minutes=0):
    """A fake 'now', expressed as an offset from BASE."""
    return BASE + timedelta(days=days, hours=hours, minutes=minutes)


def day(n):
    """The calendar date n days after BASE (used for expected run dates)."""
    return (BASE + timedelta(days=n)).date()


class FakeMarker:
    """In-memory stand-in for a MarkerStore, for deterministic tests.

    `done` is the set of logical days considered complete. Set `raise_on_check`
    to simulate a marker backend that errors, to exercise the watchdog's
    fail-open guard.
    """

    def __init__(self):
        self.done = set()
        self.raise_on_check = False
        self.marked = []          # records mark_done() calls, in order

    def is_done(self, logical_day):
        if self.raise_on_check:
            raise RuntimeError("marker backend unreachable")
        return logical_day in self.done

    def mark_done(self, logical_day, meta=None):
        self.done.add(logical_day)
        self.marked.append(logical_day)


def _make(clock_ref, awake_ref, calls, *, day_start=dtime(0, 0),
          cutoff=dtime(23, 30), marker=None):
    return DailyWatchdog(
        job=lambda run_date: calls.append(run_date),
        is_awake=lambda: awake_ref[0],
        cutoff=cutoff,
        day_start=day_start,
        tz=TZ,
        clock=lambda: clock_ref[0],
        marker=marker,
    )


# A full-window cutoff for a 16:00 day: 15:59 resolves to the NEXT day's 15:59,
# the minute before rollover, so the whole day is usable.
FULL_DAY = dtime(15, 59)


# --- general state-machine behaviour (day_start defaults to 00:00) -----------

def test_waits_then_runs_when_awake():
    clock = [at(hours=6)]                 # BASE day, 06:00
    awake = [False]
    calls = []
    wd = _make(clock, awake, calls)

    assert wd.tick() is State.PENDING     # not reachable yet
    awake[0] = True
    assert wd.tick() is State.DONE        # reachable -> runs
    assert calls == [day(0)]

    assert wd.tick() is State.DONE        # stays done, no re-run
    assert len(calls) == 1


def test_missed_after_cutoff():
    clock = [at(hours=23, minutes=45)]    # 23:45, past the 23:30 cutoff
    wd = _make(clock, [False], [])
    assert wd.tick() is State.MISSED


def test_new_day_re_arms():
    clock = [at(hours=6)]                 # BASE day, 06:00
    calls = []
    wd = _make(clock, [True], calls)
    assert wd.tick() is State.DONE
    clock[0] = at(days=1, hours=6)        # next day, 06:00
    assert wd.tick() is State.DONE
    assert calls == [day(0), day(1)]


def test_retries_on_job_failure():
    clock = [at(hours=6)]
    attempts = {"n": 0}

    def flaky(_run_date):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient failure")

    wd = DailyWatchdog(
        job=flaky, is_awake=lambda: True,
        cutoff=dtime(23, 30), tz=TZ, clock=lambda: clock[0],
    )
    assert wd.tick() is State.PENDING     # failed -> back to pending
    assert wd.tick() is State.DONE        # retried -> succeeds
    assert attempts["n"] == 2


# --- shifted day: day_start = 16:00 UTC -------------------------------------

def test_before_boundary_belongs_to_previous_watchdog_day():
    # 10:00 on BASE+1 still belongs to the window that opened 16:00 on BASE.
    clock = [at(days=1, hours=10)]
    calls = []
    wd = _make(clock, [True], calls, day_start=dtime(16, 0), cutoff=FULL_DAY)
    assert wd.tick() is State.DONE
    assert calls == [day(0)]              # labelled by the day the window opened


def test_rearms_at_1600_boundary():
    clock = [at(days=1, hours=10)]        # BASE+1 10:00 -> BASE window
    calls = []
    wd = _make(clock, [True], calls, day_start=dtime(16, 0), cutoff=FULL_DAY)
    assert wd.tick() is State.DONE                 # runs for the BASE window
    clock[0] = at(days=1, hours=16)               # crosses 16:00 -> new window
    assert wd.tick() is State.DONE                 # re-armed, runs for BASE+1
    assert calls == [day(0), day(1)]


def test_evening_and_next_morning_are_one_watchdog_day():
    # A run at 18:00 covers the window through 15:59 next day: no second run.
    clock = [at(hours=18)]                # BASE 18:00
    calls = []
    wd = _make(clock, [True], calls, day_start=dtime(16, 0), cutoff=FULL_DAY)
    assert wd.tick() is State.DONE
    clock[0] = at(days=1, hours=2)        # BASE+1 02:00, same window
    assert wd.tick() is State.DONE        # still done, no re-run
    assert calls == [day(0)]


def test_explicit_2330_cutoff_closes_window_the_same_evening():
    # An explicit 23:30 cutoff with a 16:00 start gives only a 16:00->23:30
    # window, so by next afternoon the day is MISSED.
    clock = [at(days=1, hours=15, minutes=59)]    # next afternoon, Pi awake
    wd = _make(clock, [True], [], day_start=dtime(16, 0), cutoff=dtime(23, 30))
    assert wd.tick() is State.MISSED


def test_default_cutoff_is_full_watchdog_day():
    # No cutoff -> derived minute-before-next-16:00, so the whole window works.
    clock = [at(days=1, hours=10)]        # ~18h into the BASE window
    calls = []
    wd = DailyWatchdog(
        job=lambda d: calls.append(d), is_awake=lambda: True,
        day_start=dtime(16, 0), tz=TZ, clock=lambda: clock[0],
    )
    assert wd.tick() is State.DONE
    assert calls == [day(0)]


def test_default_cutoff_misses_only_at_window_end():
    clock = [at(days=1, hours=15, minutes=59)]    # the rollover minute, Pi down
    wd = DailyWatchdog(
        job=lambda d: None, is_awake=lambda: False,
        day_start=dtime(16, 0), tz=TZ, clock=lambda: clock[0],
    )
    assert wd.tick() is State.MISSED


def test_cutoff_resolves_after_midnight_in_shifted_day():
    # day_start 16:00, cutoff 02:00 -> cutoff is 02:00 the NEXT calendar day.
    clock = [at(hours=23)]                         # BASE 23:00, before cutoff
    wd = _make(clock, [False], [], day_start=dtime(16, 0), cutoff=dtime(2, 0))
    assert wd.tick() is State.PENDING
    clock[0] = at(days=1, hours=2, minutes=30)     # BASE+1 02:30, past cutoff
    assert wd.tick() is State.MISSED


# --- exact production config (day_start=16:00, cutoff derived) ---------------

def test_production_config_full_run():
    # Mirrors the deployed Container App: WATCHDOG_DAY_START=16:00, no cutoff.
    # Pi becomes reachable at 20:00 UTC -> runs for that watchdog day.
    clock = [at(hours=20)]               # BASE 20:00
    calls = []
    wd = DailyWatchdog(
        job=lambda d: calls.append(d), is_awake=lambda: True,
        day_start=dtime(16, 0), tz=TZ, clock=lambda: clock[0],
    )
    assert wd.tick() is State.DONE
    assert calls == [day(0)]


# --- persistent done-marker (blob mode) -------------------------------------

def test_marker_absent_runs_and_writes_marker():
    # No marker yet -> run, and record the day as done in the marker store.
    clock = [at(hours=6)]
    calls = []
    marker = FakeMarker()
    wd = _make(clock, [True], calls, marker=marker)
    assert wd.tick() is State.DONE
    assert calls == [day(0)]
    assert day(0) in marker.done          # marker written on success
    assert marker.marked == [day(0)]


def test_marker_present_skips_run():
    # Marker already says done (e.g. a prior container ran) -> do NOT re-run.
    clock = [at(hours=6)]
    calls = []
    marker = FakeMarker()
    marker.done.add(day(0))
    wd = _make(clock, [True], calls, marker=marker)
    assert wd.tick() is State.DONE
    assert calls == []                    # skipped: marker is authoritative


def test_marker_survives_simulated_redeploy():
    # A fresh watchdog instance (new container) with the SAME marker store must
    # NOT re-run a day the marker already records as done.
    clock = [at(hours=6)]
    marker = FakeMarker()

    calls_a = []
    wd_a = _make(clock, [True], calls_a, marker=marker)
    assert wd_a.tick() is State.DONE
    assert calls_a == [day(0)]

    # "redeploy": brand-new instance, same durable marker, same day.
    calls_b = []
    wd_b = _make(clock, [True], calls_b, marker=marker)
    assert wd_b.tick() is State.DONE
    assert calls_b == []                  # did not re-run after redeploy


def test_cleared_marker_forces_rerun_on_live_container():
    # The manual override: delete/rename the marker -> next tick re-runs, even
    # though the same long-lived instance already completed the day.
    clock = [at(hours=6)]
    calls = []
    marker = FakeMarker()
    wd = _make(clock, [True], calls, marker=marker)

    assert wd.tick() is State.DONE
    assert calls == [day(0)]

    marker.done.discard(day(0))           # operator deletes the marker
    assert wd.tick() is State.DONE        # re-armed by the missing marker
    assert calls == [day(0), day(0)]      # ran again for the same day


def test_marker_error_fails_open_and_runs():
    # If the marker backend errors, treat as NOT done and run (fail-open),
    # rather than silently skipping the day.
    clock = [at(hours=6)]
    calls = []
    marker = FakeMarker()
    marker.raise_on_check = True
    wd = _make(clock, [True], calls, marker=marker)
    assert wd.tick() is State.DONE        # ran despite the marker read error
    assert calls == [day(0)]


def test_no_marker_keeps_in_memory_behaviour():
    # With marker=None (local mode), a fresh instance re-runs the day — the
    # original in-memory behaviour, unchanged.
    clock = [at(hours=6)]

    calls_a = []
    wd_a = _make(clock, [True], calls_a)          # no marker
    assert wd_a.tick() is State.DONE
    assert calls_a == [day(0)]

    calls_b = []
    wd_b = _make(clock, [True], calls_b)          # "redeploy", no durable state
    assert wd_b.tick() is State.DONE
    assert calls_b == [day(0)]                     # re-runs, as before
