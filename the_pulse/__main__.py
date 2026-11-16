"""Usage: python -m the_pulse <job> [--dry-run]

Jobs:
  run           long-running scheduler (what the Docker container runs)
  schedule      print the effective notification times and exit
  briefing      morning briefing: weather, power, holidays, KES rates, headlines
  kplc          check KPLC for new planned outages in your areas
  kplc-remind   send tonight's "power off tomorrow" reminder
  rain-morning  standalone rain alert for today (off by default; the briefing covers it)
  rain-evening  re-check the ride home (run ~15:00)
  news          send a news digest
  nse-daily     NSE close: blue-chip signals that changed today (trading days)
  nse-weekly    NSE weekly review: every watched stock, stance + reasons
  test          send a test message
"""
import argparse
import logging
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler

from . import briefing, kplc, news, nse, rain
from .core import DATA, ROOT, Health, JobWarning, WhatsApp, load_config, now

JOBS = {
    "briefing": briefing.send,
    "kplc": kplc.check,
    "kplc-remind": kplc.remind,
    "rain-morning": lambda c, w: rain.check(c, w, "morning"),
    "rain-evening": lambda c, w: rain.check(c, w, "evening"),
    "news": news.digest,
    "nse-daily": lambda c, w: nse.run(c, w, "daily"),
    "nse-weekly": lambda c, w: nse.run(c, w, "weekly"),
    "test": lambda c, w: w.send("✅ The Pulse is connected and can reach you on WhatsApp."),
}

DEFAULT_SCHEDULE = {
    "briefing": ["06:00"],
    "rain-morning": [],
    "news": ["18:00"],
    "kplc": ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00"],
    "rain-evening": ["15:00"],
    "kplc-remind": ["19:05"],
    "nse-daily": ["15:45"],
    "nse-weekly": ["fri 17:00"],
}


def run_job(name, cfg, wa):
    health = Health(wa)
    try:
        n = JOBS[name](cfg, wa)
        logging.info("%s done (%s)", name, n)
        health.ok(name)
        return True
    except JobWarning as e:
        logging.warning("%s: %s", name, e)
        health.failed(name, str(e))
        return False
    except Exception as e:
        logging.exception("%s failed", name)
        health.failed(name, f"{type(e).__name__}: {e}")
        return False


TIME_RE = re.compile(r"^(?:(mon|tue|wed|thu|fri|sat|sun) +)?([01]\d|2[0-3]):([0-5]\d)$")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_times(value, source):
    """'06:45, fri 17:00' or ["06:45"] -> sorted list of '[day ]HH:MM' (24h, day optional)."""
    items = value.split(",") if isinstance(value, str) else value
    times = []
    for raw in items:
        t = " ".join(str(raw).strip().lower().split())
        if not t:
            continue
        t = re.sub(r"(^|\s)(\d):", r"\g<1>0\2:", t)  # accept 5:30 -> 05:30
        t = re.sub(r"^([a-z]{3})[a-z]*(?= )", r"\1", t)  # friday 17:00 -> fri 17:00
        if not TIME_RE.match(t):
            raise SystemExit(f"{source}: invalid time {raw!r}, use 24-hour HH:MM, optionally with a day, "
                             f"e.g. 05:30, 18:00 or fri 17:00")
        times.append(t)
    return sorted(set(times), key=lambda x: (x[-5:], x))


def slot_matches(spec, t):
    """Return today's datetime for spec ('HH:MM' or 'fri HH:MM') if it applies to t's weekday."""
    m = TIME_RE.match(spec)
    day, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
    if day and DAYS.index(day) != t.weekday():
        return None
    return t.replace(hour=h, minute=mi, second=0, microsecond=0)


def build_schedule(cfg):
    """Precedence: SCHEDULE_<JOB> env var > [schedule] in config.toml > defaults.
    An env var set to an empty string disables that job."""
    file_sched = cfg.get("schedule", {})
    for job in file_sched:
        if job not in DEFAULT_SCHEDULE:
            raise SystemExit(f"config.toml [schedule]: unknown job {job!r}")
    sched = {}
    for job, default in DEFAULT_SCHEDULE.items():
        env = "SCHEDULE_" + job.upper().replace("-", "_")
        if env in os.environ:
            times = parse_times(os.environ[env], env)
        else:
            times = parse_times(file_sched.get(job, default), f"[schedule] {job}")
        if times:
            sched[job] = times
    return sched


def scheduler(cfg, wa):
    """Fire each job at its HH:MM times (Africa/Nairobi), checking every 20s."""
    sched = build_schedule(cfg)
    for job, times in sched.items():
        logging.info("schedule %-13s %s", job, ", ".join(times))
    prev = now()  # taken before the startup job so slots during it still fire
    run_job("kplc", cfg, wa)  # index current notices right away
    heartbeat = DATA / "heartbeat"
    while True:
        time.sleep(20)
        t = now()
        # Fire every job whose slot fell in (prev, t] - robust to slow jobs and clock drift.
        due = []
        for job, times in sched.items():
            for spec in times:
                slot = slot_matches(spec, t)
                if slot and prev < slot <= t:
                    due.append((slot, job))
        for _, job in sorted(due):
            run_job(job, cfg, wa)
        prev = t
        DATA.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text(t.isoformat())


def main():
    ap = argparse.ArgumentParser(prog="the-pulse", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", choices=["run", "schedule", *JOBS])
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--config")
    a = ap.parse_args()

    handlers = [logging.StreamHandler(sys.stdout)]
    logfile = os.environ.get("PULSE_LOGFILE", str(ROOT / "logs" / "the-pulse.log"))
    if logfile:  # set PULSE_LOGFILE="" (Docker) to log to stdout only
        os.makedirs(os.path.dirname(logfile), exist_ok=True)
        handlers.append(RotatingFileHandler(logfile, maxBytes=512_000, backupCount=3))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(a.config)
    wa = WhatsApp(cfg, a.dry_run)
    if a.job == "run":
        scheduler(cfg, wa)
    elif a.job == "schedule":
        for job, times in build_schedule(cfg).items():
            print(f"{job:<13} {', '.join(times)}")
    elif not run_job(a.job, cfg, wa):
        sys.exit(1)


if __name__ == "__main__":
    main()
