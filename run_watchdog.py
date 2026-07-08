"""
Watchdog service entrypoint.

Polls every minute via APScheduler. On the first minute the Pi is reachable
within the current watchdog day, it fetches the 8 source tables and writes the
daily transient snapshot, then idles until the next day boundary.

Run locally:   python run_watchdog.py
In Azure:      same image as the API, command ["python", "run_watchdog.py"]

Env:
  WATCHDOG_TZ         IANA timezone for the day boundary and cutoff (default UTC)
  WATCHDOG_DAY_START  HH:MM at which a new watchdog day begins (default 00:00).
                      Set to 16:00 for a day that runs 16:00 -> 15:59 next day.
  WATCHDOG_CUTOFF     HH:MM after which a day with no successful run is MISSED
                      (default 23:30). NOTE: this is resolved to the first
                      occurrence at or after WATCHDOG_DAY_START, so with a
                      16:00 start you almost certainly want a later cutoff
                      (e.g. 15:59) to keep the whole day usable.
  SINK / SINK_ROOT / BLOB_* : see app/sinks.py
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from app.pi_data import fetch_all, is_pi_awake
from app.sinks import Sink, build_sink
from app.watchdog import DailyWatchdog

log = logging.getLogger("watchdog.main")


def build_daily_job(sink: Sink):
    def job(run_date):
        frames = fetch_all()
        for name, df in frames.items():
            dest = sink.write(name, df, run_date)
            log.info("Wrote %s (%d rows) -> %s", name, len(df), dest)

    return job


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tz = os.getenv("WATCHDOG_TZ", "UTC")
    dh, dm = (int(x) for x in os.getenv("WATCHDOG_DAY_START", "00:00").split(":"))

    kwargs = {}
    cutoff_env = os.getenv("WATCHDOG_CUTOFF")
    if cutoff_env:
        hh, mm = (int(x) for x in cutoff_env.split(":"))
        kwargs["cutoff"] = dtime(hh, mm)

    watchdog = DailyWatchdog(
        job=build_daily_job(build_sink()),
        is_awake=is_pi_awake,
        day_start=dtime(dh, dm),
        tz=tz,
        **kwargs,
    )

    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        watchdog.tick,
        trigger="interval",
        minutes=1,
        id="daily_watchdog",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(ZoneInfo(tz)),  # run immediately on start
    )

    # --- Future cadences register here (ad-hoc, weekly, monthly, ...). ---
    # scheduler.add_job(weekly_job, "cron", day_of_week="sun", hour=3, id="weekly")
    # scheduler.add_job(monthly_job, "cron", day=1, hour=4, id="monthly")

    log.info(
        "Watchdog up (tz=%s, day_start=%02d:%02d, cutoff=%02d:%02d).",
        tz,
        dh,
        dm,
        hh,
        mm,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
