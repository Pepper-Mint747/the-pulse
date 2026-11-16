"""NSE (Nairobi Securities Exchange) long-term / dividend signals for blue chips.

Data: afx.kwayisi.org/nse (market page = live prices for every counter + NASI;
per-stock pages = EPS, P/E, dividend per share, returns and ~10 days of closes).
We keep our own daily close history in /data so moving averages and ranges
get better the longer the bot runs.

Everything here is a transparent rule with a stated reason - signals, not advice.
"""
import html
import re
import statistics
import time

from .core import JobWarning, State, holiday_name, http, log, now

BASE = "https://afx.kwayisi.org/nse/"
DISCLAIMER = ("_Rule-based signals, not financial advice. Broker fees + levies are roughly 2% "
              "per trade, so act rarely and size positions sensibly._")

DEFAULTS = {
    "watchlist": ["SCOM", "EQTY", "KCB", "COOP", "ABSA", "SCBK", "SBIC", "NCBA",
                  "IMH", "EABL", "BAT", "KEGN"],
    "high_yield": 8.0,       # % dividend yield considered attractive
    "low_yield": 3.0,        # % yield considered thin
    "dip_pct": 10.0,         # % below moving average = possible entry
    "stretched_pct": 15.0,   # % above moving average = don't chase
    "big_move_pct": 5.0,     # daily move worth mentioning
    "falling_1yr_pct": -25.0,  # 1-year return below this = falling-knife warning
    "ma_days": 50,           # moving average length (uses what history exists until then)
    "min_ma_days": 8,        # need at least this many closes before using the MA
}

STANCE = {1: "🟢 Accumulate", 0: "⚪ Neutral", -1: "🔴 Wait / avoid"}


def opts(cfg):
    return {**DEFAULTS, **cfg.get("nse", {})}


def trading_day(cfg, d):
    return d.weekday() < 5 and not holiday_name(cfg, d)


# ---------------------------------------------------------------- parsing

def _cells(row):
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split())
            for c in re.split(r"<t[dh][^>]*>", row)[1:]]


def _num(s):
    s = (s or "").replace(",", "").replace("%", "").replace("+", "").strip()
    m = re.match(r"^-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _scaled(s):
    """'1.4T' / '40.1B' / '3.23M' -> float"""
    m = re.match(r"^([\d.,]+)\s*([KMBT]?)", (s or "").strip())
    if not m:
        return None
    mult = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2)]
    return float(m.group(1).replace(",", "")) * mult


def parse_market(page):
    """-> (prices {ticker: {name, price, change, volume}}, nasi {value, change})"""
    prices, nasi = {}, None
    rows = [_cells(r) for r in page.split("<tr")[1:]]
    for r in rows:
        if nasi is None and r and re.match(r"^[\d,.]+\s*\([+-]?[\d.]+\)", r[0]):
            m = re.match(r"^([\d,.]+)\s*\(([+-]?[\d.]+)\)", r[0])
            nasi = {"value": _num(m.group(1)), "change": _num(m.group(2))}
        if len(r) >= 5 and re.fullmatch(r"[A-Z][A-Z0-9-]{1,9}", r[0]) and _num(r[3]) is not None:
            prices[r[0]] = {"name": r[1], "volume": _num(r[2]) or 0,
                            "price": _num(r[3]), "change": _num(r[4].split()[0]) if r[4] else 0.0}
    return prices, nasi


def parse_stock(page):
    """Per-stock page -> fundamentals, returns and recent closes."""
    out = {"history": {}, "returns": {}}
    rows = [_cells(r) for r in page.split("<tr")[1:]]
    labels = {
        "Earnings Per Share": "eps", "Price/Earning Ratio": "pe",
        "Dividend Per Share": "dps", "Dividend Yield": "yield",
    }
    for i, r in enumerate(rows):
        if len(r) >= 2 and r[0] in labels:
            out[labels[r[0]]] = _num(r[1])
        elif len(r) >= 2 and r[0] == "Market Capitalization":
            out["mcap"] = _scaled(r[1])
        elif r[:3] in (["1WK", "4WK", "3MO"], ["6MO", "1YR", "YTD"]) and i + 1 < len(rows):
            for k, v in zip(r[:3], rows[i + 1][:3]):
                out["returns"][k] = _num(v.split()[0]) if v else None
        elif len(r) >= 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0]) and _num(r[2]) is not None:
            out["history"][r[0]] = _num(r[2])
    m = re.search(r"<dt[^>]*>\s*Sector\s*(?:</dt>)?\s*<dd[^>]*>(.*?)<", page, re.S)
    out["sector"] = html.unescape(m.group(1)).strip() if m else "Other"
    return out


# ---------------------------------------------------------------- data

def fetch(cfg, refresh_all=False):
    """One request for live prices; per-stock fundamentals come from a cache that is
    refreshed every `fundamentals_days` (or on refresh_all, i.e. the weekly review).
    Fundamentals change a few times a year, so there's no need to hammer a free site."""
    o = opts(cfg)
    s = http()
    try:
        r = s.get(BASE, timeout=30)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Could not load NSE prices from {BASE}: {e}") from e
    prices, nasi = parse_market(r.text)
    if len(prices) < 30:
        raise JobWarning(f"NSE market page parsed only {len(prices)} counters. The site layout may have changed: {BASE}")

    st = State("nse")
    cache = st.data.setdefault("fundamentals", {})
    max_age = o.get("fundamentals_days", 3) * 86400
    stocks, missing, stale = {}, [], []
    for t in o["watchlist"]:
        if t not in prices:
            missing.append(t)
            continue
        c = cache.get(t)
        fresh = c and (time.time() - c.get("fetched", 0)) < max_age
        if refresh_all or not fresh:
            try:
                rr = s.get(f"{BASE}{t.lower()}.html", timeout=30)
                rr.raise_for_status()
                c = {**parse_stock(rr.text), "fetched": time.time()}
                cache[t] = c
            except Exception as e:
                log.warning("NSE %s page failed: %s", t, e)
                stale.append(t)
            time.sleep(2)  # be polite to a free site
        base = c or {"history": {}, "returns": {}, "sector": "Other"}
        stocks[t] = {**base, **prices[t]}
    st.save()
    if stale:
        log.warning("NSE: using cached/partial fundamentals for %s", ", ".join(stale))
    return prices, nasi, stocks, missing


def update_history(stocks, nasi, today, market_closed):
    """Merge site history + today's close into our own store. Returns {ticker: [(date, close)...]}"""
    st = State("nse")
    hist = st.data.setdefault("history", {})
    for t, d in stocks.items():
        h = hist.setdefault(t, {})
        h.update({k: v for k, v in d["history"].items() if k not in h})
        if market_closed:
            h[today.isoformat()] = d["price"]
        # keep ~400 trading days
        for k in sorted(h)[:-400]:
            del h[k]
    if nasi and market_closed:
        st.data.setdefault("nasi", {})[today.isoformat()] = nasi["value"]
    st.save()
    return {t: sorted(hist.get(t, {}).items()) for t in stocks}


# ---------------------------------------------------------------- analysis

def analyse(stocks, series, cfg):
    o = opts(cfg)
    # sector P/E medians across the watchlist (only meaningful with >= 3 peers)
    by_sector = {}
    for d in stocks.values():
        if d.get("pe") and d["pe"] > 0:
            by_sector.setdefault(d.get("sector", "Other"), []).append(d["pe"])
    pe_median = {k: statistics.median(v) for k, v in by_sector.items() if len(v) >= 3}

    results = {}
    for t, d in stocks.items():
        price = d["price"]
        reasons, score, flags = [], 0, []
        # dividend yield, recomputed at today's price
        yld = round(d["dps"] / price * 100, 1) if d.get("dps") and price else d.get("yield")
        if yld is not None:
            if yld >= o["high_yield"]:
                score += 1
                reasons.append(f"dividend yield {yld}% (≥{o['high_yield']:g}%)")
                flags.append("yield")
            elif yld < o["low_yield"]:
                score -= 1
                reasons.append(f"thin yield {yld}%")
        # P/E vs sector peers
        pe, med = d.get("pe"), pe_median.get(d.get("sector"))
        if pe and pe > 0 and med:
            if pe <= med * 0.8:
                score += 1
                reasons.append(f"P/E {pe:g} vs sector {med:.1f} (cheap)")
                flags.append("cheap")
            elif pe >= med * 1.3:
                score -= 1
                reasons.append(f"P/E {pe:g} vs sector {med:.1f} (pricey)")
        elif pe is not None and pe <= 0:
            reasons.append("loss-making (negative EPS)")
            score -= 1
        # price vs moving average from our own history
        closes = [c for _, c in series.get(t, [])][-o["ma_days"]:]
        ma = gap = None
        if len(closes) >= o["min_ma_days"]:
            ma = statistics.fmean(closes)
            gap = (price - ma) / ma * 100
            n = len(closes)
            if gap <= -o["dip_pct"]:
                score += 1
                reasons.append(f"{abs(gap):.0f}% below its {n}-day average (dip)")
                flags.append("dip")
            elif gap >= o["stretched_pct"]:
                score -= 1
                reasons.append(f"{gap:.0f}% above its {n}-day average (stretched)")
                flags.append("stretched")
        # warnings that don't change the score
        warn = []
        r1 = d.get("returns", {}).get("1YR")
        if r1 is not None and r1 <= o["falling_1yr_pct"]:
            warn.append(f"down {abs(r1):.0f}% over 1 year: check the news before buying a 'cheap' falling stock")
        prev = price - (d.get("change") or 0)
        day = (d.get("change") or 0) / prev * 100 if prev else 0
        if abs(day) >= o["big_move_pct"] and d.get("volume", 0) > 0:
            warn.append(f"moved {day:+.1f}% today")
            flags.append("bigmove")
        stance = 1 if score >= 2 else (-1 if score <= -1 else 0)
        results[t] = {
            "name": d["name"], "price": price, "day_pct": round(day, 2), "yield": yld, "pe": pe,
            "ma": round(ma, 2) if ma else None, "gap": round(gap, 1) if gap is not None else None,
            "history_days": len(closes), "returns": d.get("returns", {}),
            "score": score, "stance": stance, "reasons": reasons, "warn": warn, "flags": sorted(flags),
        }
    return results


# ---------------------------------------------------------------- messages

def _line(t, r, detail=True):
    s = f"*{t}* {r['price']:,.2f} ({r['day_pct']:+.1f}%) · {STANCE[r['stance']]}"
    if detail and r["reasons"]:
        s += "\n   _" + "; ".join(r["reasons"]) + "_"
    if detail:
        for w in r["warn"]:
            s += f"\n   ⚠️ {w}"
    return s


def run(cfg, wa, mode):
    """mode = 'daily' (only changes) or 'weekly' (full review)."""
    t0 = now()
    today = t0.date()
    is_trading = trading_day(cfg, today)
    if mode == "daily" and not is_trading:
        log.info("NSE closed today (%s)", holiday_name(cfg, today) or today.strftime("%A"))
        return 0
    market_closed = is_trading and t0.hour * 60 + t0.minute >= 15 * 60 + 15
    prices, nasi, stocks, missing = fetch(cfg, refresh_all=(mode == "weekly"))
    series = update_history(stocks, nasi, today, market_closed)
    res = analyse(stocks, series, cfg)

    st = State("nse")
    prev = st.data.get("last", {})
    st.data["last"] = {t: {"stance": r["stance"], "flags": r["flags"]} for t, r in res.items()}
    st.save()

    nasi_line = (f"📊 NASI {nasi['value']:,.2f} ({nasi['change']:+.2f})" if nasi else "")
    if mode == "daily":
        changed = []
        for t, r in res.items():
            p = prev.get(t)
            new_flags = set(r["flags"]) - set(p["flags"]) if p else set()
            if p is None or p["stance"] != r["stance"] or new_flags & {"dip", "stretched", "bigmove", "yield", "cheap"}:
                if p is not None or r["stance"] != 0:
                    changed.append((t, r, p))
        if not changed:
            log.info("NSE: no signal changes today")
            return 0
        lines = [f"📈 *NSE close · {t0:%a %d %b}*", nasi_line, ""]
        for t, r, p in changed:
            was = f" (was {STANCE[p['stance']].split(' ', 1)[1]})" if p and p["stance"] != r["stance"] else ""
            lines.append(_line(t, r) + was)
        lines += ["", DISCLAIMER]
        wa.send("\n".join(x for x in lines if x is not None))
        return len(changed)

    # weekly review
    order = sorted(res.items(), key=lambda kv: (-kv[1]["stance"], -kv[1]["score"], -(kv[1]["yield"] or 0)))
    lines = [f"🗓 *NSE weekly review · {t0:%d %b %Y}*", nasi_line]
    for label, s in ((STANCE[1], 1), (STANCE[0], 0), (STANCE[-1], -1)):
        group = [(t, r) for t, r in order if r["stance"] == s]
        if group:
            lines += ["", f"*{label}*"] + [_line(t, r) for t, r in group]
    yields = sorted(((r["yield"] or 0, t) for t, r in res.items()), reverse=True)[:5]
    lines += ["", "💰 *Top dividend yields:* " + " · ".join(f"{t} {y:g}%" for y, t in yields if y)]
    perf = sorted(((r["returns"].get("1WK"), t) for t, r in res.items() if r["returns"].get("1WK") is not None),
                  reverse=True)
    if perf:
        lines.append(f"📅 *This week:* best {perf[0][1]} {perf[0][0]:+.1f}% · worst {perf[-1][1]} {perf[-1][0]:+.1f}%")
    short = [t for t, r in res.items() if r["history_days"] < opts(cfg)["ma_days"]]
    if short:
        days = min(r["history_days"] for r in res.values())
        lines.append(f"_Moving averages still building ({days}/{opts(cfg)['ma_days']} days of history), "
                     "so dip/stretched signals get more reliable over the coming weeks._")
    if missing:
        lines.append(f"_Not found on the NSE list: {', '.join(missing)}_")
    lines += ["", DISCLAIMER]
    wa.send("\n".join(lines))
    return len(res)
