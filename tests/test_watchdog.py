from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from app.watchdog import DailyWatchdog, State

TZ = "UTC"


def _dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ))


def _make(clock_ref, awake_ref, calls, *, day_start=dtime(0, 0), cutoff=dtime(23, 30)):
    return DailyWatchdog(
        job=lambda run_date: calls.append(run_date),
        is_awake=lambda: awake_ref[0],
        cutoff=cutoff,
        day_start=day_start,
        tz=TZ,
        clock=lambda: clock_ref[0],
    )


# --- original behaviour (day_start defaults to 00:00) -----------------------


def test_waits_then_runs_when_awake():
    clock = [_dt(2026, 7, 7, 6, 0)]
    awake = [False]
    calls = []
    wd = _make(clock, awake, calls)

    assert wd.tick() is State.PENDING  # not reachable yet
    awake[0] = True
    assert wd.tick() is State.DONE  # reachable -> runs
    assert calls == [clock[0].date()]

    assert wd.tick() is State.DONE  # stays done, no re-run
    assert len(calls) == 1


def test_missed_after_cutoff():
    clock = [_dt(2026, 7, 7, 23, 45)]
    wd = _make(clock, [False], [])
    assert wd.tick() is State.MISSED


def test_new_day_re_arms():
    clock = [_dt(2026, 7, 7, 6, 0)]
    calls = []
    wd = _make(clock, [True], calls)
    assert wd.tick() is State.DONE
    clock[0] = _dt(2026, 7, 8, 6, 0)  # next day
    assert wd.tick() is State.DONE
    assert len(calls) == 2


def test_retries_on_job_failure():
    clock = [_dt(2026, 7, 7, 6, 0)]
    attempts = {"n": 0}

    def flaky(_run_date):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient failure")

    wd = DailyWatchdog(
        job=flaky,
        is_awake=lambda: True,
        cutoff=dtime(23, 30),
        tz=TZ,
        clock=lambda: clock[0],
    )
    assert wd.tick() is State.PENDING  # failed -> back to pending
    assert wd.tick() is State.DONE  # retried -> succeeds
    assert attempts["n"] == 2


# --- shifted day: day_start = 16:00 UTC -------------------------------------

# For a 16:00 day, a full-window cutoff is 15:59 (resolves to the NEXT day's
# 15:59, the minute before rollover). This is what makes the whole day usable.
FULL_DAY = dtime(15, 59)


def test_before_boundary_belongs_to_previous_watchdog_day():
    # 10:00 UTC still belongs to the day that started 16:00 the day before.
    clock = [_dt(2026, 7, 7, 10, 0)]
    calls = []
    wd = _make(clock, [True], calls, day_start=dtime(16, 0), cutoff=FULL_DAY)
    assert wd.tick() is State.DONE
    assert calls == [date(2026, 7, 6)]  # labelled by the day it started


def test_rearms_at_1600_boundary():
    clock = [_dt(2026, 7, 7, 10, 0)]
    calls = []
    wd = _make(clock, [True], calls, day_start=dtime(16, 0), cutoff=FULL_DAY)
    assert wd.tick() is State.DONE  # runs for the July 6 window
    clock[0] = _dt(2026, 7, 7, 16, 0)  # crosses 16:00 -> new window
    assert wd.tick() is State.DONE  # re-armed, runs for July 7
    assert calls == [date(2026, 7, 6), date(2026, 7, 7)]


def test_evening_and_next_morning_are_one_watchdog_day():
    # A run at 18:00 covers the window through 15:59 next day: no second run.
    clock = [_dt(2026, 7, 7, 18, 0)]
    calls = []
    wd = _make(clock, [True], calls, day_start=dtime(16, 0), cutoff=FULL_DAY)
    assert wd.tick() is State.DONE
    clock[0] = _dt(2026, 7, 8, 2, 0)  # 02:00, same window
    assert wd.tick() is State.DONE  # still done, no re-run
    assert calls == [date(2026, 7, 7)]


def test_default_cutoff_closes_window_the_same_evening():
    # Documents the gotcha: day_start 16:00 with the DEFAULT 23:30 cutoff gives
    # only a 16:00->23:30 window, so by next afternoon the day is MISSED.
    clock = [_dt(2026, 7, 7, 15, 59)]  # next afternoon, Pi awake
    wd = _make(clock, [True], [], day_start=dtime(16, 0))  # cutoff defaults 23:30
    assert wd.tick() is State.MISSED


def test_cutoff_resolves_after_midnight_in_shifted_day():
    # day_start 16:00, cutoff 02:00 -> cutoff is 02:00 the NEXT calendar day.
    clock = [_dt(2026, 7, 7, 23, 0)]  # before the cutoff
    wd = _make(clock, [False], [], day_start=dtime(16, 0), cutoff=dtime(2, 0))
    assert wd.tick() is State.PENDING
    clock[0] = _dt(2026, 7, 8, 2, 30)  # past the cutoff
    assert wd.tick() is State.MISSED
