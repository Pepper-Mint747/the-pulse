"""Kenyan news digest from RSS feeds."""
import calendar
import re
import time
from datetime import timedelta
from urllib.parse import urlparse

import feedparser

from .core import State, http, log, now

SOURCES = {
    "nation.africa": "Nation", "standardmedia.co.ke": "Standard",
    "capitalfm.co.ke": "Capital FM", "kenyans.co.ke": "Kenyans.co.ke",
}


def source_name(url):
    host = urlparse(url).netloc.removeprefix("www.")
    return SOURCES.get(host, host)


def clean_link(url):
    """Drop utm_* tracking params so links are shorter in WhatsApp."""
    u = urlparse(url)
    q = "&".join(p for p in u.query.split("&") if p and not p.startswith("utm_"))
    return u._replace(query=q).geturl()


def norm(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())


def similar(a, b):
    wa, wb = set(norm(a).split()), set(norm(b).split())
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def fetch(cfg):
    s = http()
    cutoff = time.time() - cfg["lookback_hours"] * 3600
    items, failed = [], []
    for i, feed in enumerate(cfg["feeds"]):
        try:
            parsed = feedparser.parse(s.get(feed, timeout=30).content)
        except Exception as e:
            log.warning("feed %s failed: %s", feed, e)
            failed.append(f"{feed}: {e}")
            continue
        for rank, e in enumerate(parsed.entries[:30]):
            t = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(t) if t else None
            if ts and ts < cutoff:
                continue
            title = " ".join(e.get("title", "").split())
            if title and e.get("link"):
                items.append({"title": title, "link": clean_link(e.link), "src": source_name(feed),
                              "ts": ts or 0, "rank": rank, "feed": i})
    if failed and len(failed) == len(cfg["feeds"]):
        raise RuntimeError("All news feeds failed:\n" + "\n".join(failed))
    return items


def pick(items, cfg, sent, limit):
    kws = [re.compile(rf"\b{re.escape(k)}\b", re.I) for k in cfg.get("keywords", [])]
    for it in items:
        it["star"] = any(k.search(it["title"]) for k in kws)
    # Starred first, then each feed's own top stories (interleaved), then newest.
    items.sort(key=lambda it: (not it["star"], it["rank"], -it["ts"]))
    chosen = []
    for it in items:
        if it["link"] in sent or any(similar(it["title"], c["title"]) for c in chosen):
            continue
        chosen.append(it)
        if len(chosen) >= limit:
            break
    return chosen


def headlines(cfg, limit=None):
    """Pick unseen stories. Returns (lines, commit) - call commit() after sending."""
    nc = cfg["news"]
    st = State("news")
    sent = st.data.setdefault("sent", {})
    chosen = pick(fetch(nc), nc, sent, limit or nc["max_items"])
    lines = []
    for n, it in enumerate(chosen, 1):
        star = "⭐ " if it["star"] else ""
        lines.append(f"*{n}. {star}{it['title']}*\n_{it['src']}_ · {it['link']}")

    def commit():
        t = now()
        for it in chosen:
            sent[it["link"]] = t.isoformat()
        old = (t - timedelta(days=3)).isoformat()
        st.data["sent"] = {k: v for k, v in sent.items() if v >= old}
        st.save()

    return lines, commit


def digest(cfg, wa):
    lines, commit = headlines(cfg)
    if not lines:
        return 0
    t = now()
    head = "🌅 *Morning news*" if t.hour < 12 else "🌆 *Evening news*"
    wa.send(f"{head} · {t:%a %d %b}\n\n" + "\n\n".join(lines))
    commit()
    return len(lines)
