"""KPLC planned power interruptions -> WhatsApp alerts."""
import hashlib
import html
import io
import re
from datetime import date, timedelta
from urllib.parse import urljoin

from pypdf import PdfReader

from .core import JobWarning, State, http, log, now

# Where KPLC lists its notice PDFs. Override with [kplc] pages in config.toml.
# /power-maintenance-schedule appeared (empty) during KPLC's Nov 2026 site rework.
NOTICE_PAGES = [
    "https://www.kplc.co.ke/customer-support",
    "https://www.kplc.co.ke/power-maintenance-schedule",
]
LINK_RE = re.compile(
    r'<a[^>]+href="((?:https?://(?:www\.)?kplc\.co\.ke)?/storage/[^"]+\.pdf)"[^>]*>(.*?)</a>', re.S | re.I
)
DATE_RE = re.compile(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})")


def squash(s):
    """Lowercase and drop all whitespace: PDF extraction splits words ("Ma rtins")."""
    return re.sub(r"\s+", "", s).lower()


def tidy(s):
    return " ".join(s.split())


def list_notices(session, pages):
    """Collect (pdf_url, title) from every notice page; one broken page doesn't stop the others."""
    seen, out, errors = set(), [], []
    for page_url in pages:
        try:
            r = session.get(page_url, timeout=60)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"{page_url}: {e}")
            continue
        for href, inner in LINK_RE.findall(r.text):
            url = urljoin(page_url, html.unescape(href))
            title = tidy(html.unescape(re.sub(r"<[^>]+>", " ", inner)))
            if url in seen or not re.search(r"notice|interruption|maintenance", title, re.I):
                continue
            seen.add(url)
            out.append((url, title))
    return out, errors


def parse_pdf(data, with_size=False):
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    entries = []
    for block in re.split(r"(?=AREA\s*:)", text)[1:]:
        m = re.match(r"AREA\s*:\s*(.*?)\s*DATE\s*:\s*(.*?)\s*TIME\s*:\s*(.*?)\n(.*)", block, re.S)
        if not m:
            continue
        area, date_s, time_s, rest = m.groups()
        # Places run until "& adjacent customers." – anything after is the next region header.
        places = re.split(r"adja\w*cent\s*customers\.?", rest, maxsplit=1, flags=re.I)[0]
        entries.append({
            "area": tidy(area), "date": tidy(date_s), "time": tidy(time_s),
            "places": tidy(places).rstrip(" &,"), "raw": block,
        })
    return (entries, len(text.strip())) if with_size else entries


def parse_date(s):
    m = DATE_RE.search(s)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return date(y, mth, d)
    except ValueError:  # KPLC typos like 14.14.2026
        return None


def classify(entry, cfg):
    blob = squash(entry["area"] + " " + entry["places"])
    for tier in ("home", "work", "nearby"):
        hits = [k for k in cfg[f"{tier}_keywords"] if squash(k) in blob]
        if hits:
            return tier, hits
    return None, []


LABEL = {"home": "🏠 HOME", "work": "💼 WORK", "nearby": "📍 Near home"}


def fmt(entry, tier, hits, cfg_loc):
    where = {"home": cfg_loc["home"]["name"], "work": cfg_loc["work"]["name"]}.get(tier, ", ".join(hits))
    return (
        f"{LABEL[tier]} ({where})\n"
        f"📅 {entry['date']}\n⏰ {entry['time']}\n"
        f"📌 {entry['area'].title()}\n_{entry['places']}_"
    )


def check(cfg, wa):
    """Fetch new KPLC notices; alert on any matching area."""
    st = State("kplc")
    done = st.data.setdefault("pdfs", {})
    upcoming = st.data.setdefault("upcoming", {})
    s = http()
    pages = cfg["kplc"].get("pages") or NOTICE_PAGES
    notices, page_errors = list_notices(s, pages)
    if not notices:
        detail = ("\n" + "\n".join(page_errors)) if page_errors else ""
        raise JobWarning("No outage notices listed on KPLC's site right now (checked "
                         + ", ".join(p.split("kplc.co.ke")[-1] for p in pages)
                         + "). KPLC may have moved or removed them." + detail)
    first_run = not done
    problems = []
    today = now().date()
    alerts = []
    for url, title in notices:
        if url in done:
            continue
        try:
            r = s.get(url, timeout=120)
            if r.status_code != 200 or not r.content.startswith(b"%PDF"):
                raise RuntimeError(f"KPLC's site returned HTTP {r.status_code} "
                                   f"({r.headers.get('content-type', '?')}) instead of the PDF. "
                                   f"Will retry every run. {url}")
            entries, chars = parse_pdf(r.content, with_size=True)
        except Exception as e:  # one bad PDF shouldn't block the rest; retried next run
            log.warning("failed %s: %s", url, e)
            problems.append(f"Could not read '{title}': {e}")
            continue
        log.info("%s: %d areas", title, len(entries))
        if not entries and chars > 300:
            problems.append(f"'{title}' has text but no AREA/DATE/TIME entries. "
                            f"The PDF format may have changed: {url}")
        for e in entries:
            tier, hits = classify(e, cfg["kplc"])
            if not tier:
                continue
            d = parse_date(e["date"])
            # Skip past outages. Unparseable dates (KPLC typos) are only trusted
            # once we're past the first run, i.e. for freshly published notices.
            if (d and d < today) or (d is None and first_run):
                continue
            key = hashlib.sha1((e["area"] + e["date"] + e["time"]).encode()).hexdigest()[:12]
            if key in upcoming:
                continue
            upcoming[key] = {"date": d.isoformat() if d else None, "text": fmt(e, tier, hits, cfg["locations"]),
                             "tier": tier, "reminded": False}
            alerts.append(upcoming[key])
        done[url] = title
    if alerts:
        order = {"home": 0, "work": 1, "nearby": 2}
        alerts.sort(key=lambda a: (order[a["tier"]], a["date"] or ""))
        msg = "⚡ *KPLC planned power interruption*\n\n" + "\n\n".join(a["text"] for a in alerts)
        msg += "\n\n_Source: kplc.co.ke · I'll remind you the evening before._"
        wa.send(msg)
    elif first_run:
        log.info("first run: indexed %d notices, nothing upcoming for your areas", len(notices))
    # prune old
    for k in [k for k, v in upcoming.items() if v["date"] and v["date"] < (today - timedelta(days=2)).isoformat()]:
        del upcoming[k]
    st.save()
    if problems:
        raise JobWarning("\n".join(problems))
    return len(alerts)


def outages_on(day):
    """Known upcoming outages (already alerted) for a given date, for the morning briefing."""
    return [v for v in State("kplc").data.get("upcoming", {}).values() if v["date"] == day.isoformat()]


def remind(cfg, wa):
    """Evening-before reminder for tomorrow's outages."""
    st = State("kplc")
    tomorrow = (now().date() + timedelta(days=1)).isoformat()
    due = [v for v in st.data.get("upcoming", {}).values() if v["date"] == tomorrow and not v["reminded"]]
    if due:
        msg = "🔌 *Reminder: power goes off TOMORROW*\nCharge your phone, laptop & power bank tonight.\n\n"
        wa.send(msg + "\n\n".join(v["text"] for v in due))
        for v in due:
            v["reminded"] = True
        st.save()
    return len(due)
