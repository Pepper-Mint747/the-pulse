"""Shared plumbing: config, persistent state, HTTP session, WhatsApp sender."""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

if sys.version_info >= (3, 11):
    import tomllib
else:  # Raspberry Pi OS Bullseye ships Python 3.9
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("PULSE_DATA", ROOT / "data"))
TZ = ZoneInfo("Africa/Nairobi")
log = logging.getLogger("the-pulse")


def now():
    return datetime.now(TZ)


def load_config(path=None):
    path = Path(path or os.environ.get("PULSE_CONFIG", ROOT / "config.toml"))
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    # Env vars win, so secrets can live in docker-compose / .env instead of the file.
    wa = cfg.setdefault("whatsapp", {})
    for key, env in (("api_url", "WA_API_URL"), ("api_key", "WA_API_KEY"), ("to", "WA_TO"),
                     ("session", "WA_SESSION")):
        if os.environ.get(env):
            wa[key] = os.environ[env]
    return cfg


def http():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux armv7l) the-pulse/1.0"
    return s


class State:
    """Tiny JSON store so we never send the same alert twice."""

    def __init__(self, name):
        DATA.mkdir(parents=True, exist_ok=True)
        self.path = DATA / f"{name}.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        tmp.replace(self.path)


DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_holiday_cache = {}


def holiday_name(cfg, d):
    """Kenyan public holiday name for date d (plus [calendar] extra_holidays), else None."""
    cal = cfg.get("calendar", {})
    extra = cal.get("extra_holidays", {})
    # extra_holidays may be a list of dates or a {date: name} table
    if isinstance(extra, list):
        extra = {x: "Public holiday" for x in extra}
    if d.isoformat() in extra:
        return extra[d.isoformat()]
    if d.year not in _holiday_cache:
        try:
            import holidays
            _holiday_cache[d.year] = holidays.Kenya(years=[d.year])
        except Exception as e:  # never let the holiday lookup break an alert
            log.warning("holiday lookup failed: %s", e)
            _holiday_cache[d.year] = {}
    return _holiday_cache[d.year].get(d)


def workday(cfg, d):
    """(is_workday, reason) – reason is the weekday or holiday name when it's a day off."""
    days = [x.lower()[:3] for x in cfg.get("calendar", {}).get("workdays", DAY_NAMES[:5])]
    hol = holiday_name(cfg, d)
    if hol:
        return False, hol
    if DAY_NAMES[d.weekday()] not in days:
        return False, d.strftime("%A")
    return True, None


class Health:
    """Track job failures; WhatsApp you once a day per failing job, and again when it recovers."""

    def __init__(self, wa):
        self.wa = wa
        self.st = State("health")

    def failed(self, job, err):
        rec = self.st.data.setdefault(job, {"since": now().isoformat(), "count": 0})
        rec["count"] += 1
        rec["error"] = err[:300]
        today = now().date().isoformat()
        if rec.get("alerted") != today:
            try:
                self.wa.send(f"🚨 *The Pulse: {job} failed*\n{err[:500]}\n\n"
                             f"_Failing since {rec['since'][:16].replace('T', ' ')} "
                             f"({rec['count']}x). I'll tell you when it recovers._")
                rec["alerted"] = today
            except Exception as e:  # WhatsApp itself may be what's down
                log.warning("could not send failure alert: %s", e)
        self.st.save()

    def ok(self, job):
        rec = self.st.data.pop(job, None)
        if rec is None:
            return
        self.st.save()
        if rec.get("alerted"):
            try:
                self.wa.send(f"✅ *The Pulse: {job} is working again* (failed {rec['count']}x)")
            except Exception as e:
                log.warning("could not send recovery notice: %s", e)


class JobWarning(Exception):
    """A job finished but noticed something suspicious (e.g. KPLC changed its PDF format)."""


class WhatsApp:
    """Client for the OpenWA gateway REST API:
    POST {api_url}/api/sessions/{session_id}/messages/send-text  {"chatId", "text"}  (X-API-Key header)
    """

    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg["whatsapp"]
        self.dry_run = dry_run
        base = self.cfg.get("api_url", "").rstrip("/")
        self.base = base if base.endswith("/api") else base + "/api"
        self.headers = {"Content-Type": "application/json"}
        if self.cfg.get("api_key"):
            self.headers["X-API-Key"] = self.cfg["api_key"]
        self._session_id = None

    def _request(self, method, path, **kw):
        r = requests.request(method, self.base + path, headers=self.headers, timeout=60, **kw)
        if not r.ok:
            raise RuntimeError(f"OpenWA {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def session_id(self):
        """WA_SESSION may be a session id or name; empty = the only ready session."""
        if self._session_id:
            return self._session_id
        wanted = (self.cfg.get("session") or "").strip()
        sessions = self._request("GET", "/sessions")
        if wanted:
            match = [s for s in sessions if wanted in (s.get("id"), s.get("name"))]
        else:
            match = [s for s in sessions if s.get("status") == "ready"]
        if len(match) != 1:
            names = ", ".join(f"{s.get('name')} ({s.get('status')})" for s in sessions) or "none"
            why = f"no session named {wanted!r}" if wanted else "set WA_SESSION to pick one"
            raise RuntimeError(f"OpenWA: {why}. Sessions on server: {names}")
        s = match[0]
        if s.get("status") != "ready":
            log.warning("OpenWA session %s is %s, sending anyway", s.get("name"), s.get("status"))
        self._session_id = s["id"]
        return self._session_id

    def send(self, text):
        if self.dry_run:
            print("─" * 40 + "\n" + text + "\n" + "─" * 40)
            return
        to = self.cfg["to"].strip()
        if "@" not in to:  # allow a bare number like 2547XXXXXXXX
            to = to.lstrip("+") + "@c.us"
        res = self._request("POST", f"/sessions/{self.session_id()}/messages/send-text",
                            json={"chatId": to, "text": text})
        log.info("sent %d chars -> %s", len(text), str(res)[:120])
