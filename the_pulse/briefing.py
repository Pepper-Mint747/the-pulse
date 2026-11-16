"""One morning message: weather, power, holidays, exchange rates and headlines."""
from datetime import timedelta

from . import kplc, news, rain
from .core import JobWarning, State, holiday_name, http, log, now

FX_API = "https://open.er-api.com/v6/latest/KES"


def fx_line(cfg):
    codes = cfg.get("briefing", {}).get("currencies", ["USD", "GBP", "EUR"])
    if not codes:
        return None
    r = http().get(FX_API, timeout=30)
    r.raise_for_status()
    rates = r.json()["rates"]
    st = State("fx")
    prev = st.data.get("rates", {})
    parts = []
    for c in codes:
        kes = round(1 / rates[c], 2)  # API gives 1 KES = x foreign
        arrow = ""
        if c in prev and prev[c] != kes:
            arrow = " ▲" if kes > prev[c] else " ▼"
        parts.append(f"{c} {kes:,.2f}{arrow}")
    st.data["rates"] = {c: round(1 / rates[c], 2) for c in codes}
    st.save()
    return "💱 *KES:* " + " · ".join(parts)


def weather_block(cfg):
    fc = rain.forecast(cfg, "morning")
    if fc["workday"]:
        rain.remember_evening(fc)  # so the 15:00 re-check doesn't repeat this
    temps = f" · {fc['temps'][0]}–{fc['temps'][1]}°C" if fc["temps"] else ""
    if fc["sections"]:
        return f"☔ *Weather*{temps}\n" + rain.render(fc["sections"]) + ("\n" + rain.TIP if fc["workday"] else "")
    if fc["workday"]:
        return f"🌤 *Weather*{temps}\nDry commute both ways, no umbrella needed."
    if fc["temps"]:
        return f"🌤 *Weather*{temps}\nNo rain expected around {cfg['locations']['home']['name']} today."
    return None


def power_block(cfg):
    today = kplc.outages_on(now().date())
    if not today:
        return "🔌 No planned power outages today in your areas."
    return "⚡ *Power off TODAY*\n\n" + "\n\n".join(v["text"] for v in today)


def build(cfg):
    """Returns (message, commit_fns, problems)."""
    t = now()
    bc = cfg.get("briefing", {})
    hol_today = holiday_name(cfg, t.date())
    hol_tomorrow = holiday_name(cfg, t.date() + timedelta(days=1))

    head = f"☀️ *Good morning* · {t:%A %d %b}"
    if hol_today:
        head += f"\n🎉 Today is *{hol_today}*, a public holiday."
    blocks, problems, commits = [head], [], []

    for name, fn in (("weather", weather_block), ("power", power_block), ("fx", fx_line)):
        try:
            b = fn(cfg)
            if b:
                blocks.append(b)
        except Exception as e:
            log.warning("briefing %s failed: %s", name, e)
            problems.append(f"{name}: {e}")

    if hol_tomorrow:
        blocks.append(f"📅 Tomorrow is *{hol_tomorrow}*, a public holiday.")

    n = bc.get("news_items", 5)
    if n:
        try:
            lines, commit = news.headlines(cfg, n)
            if lines:
                blocks.append("📰 *Headlines*\n\n" + "\n\n".join(lines))
                commits.append(commit)
        except Exception as e:
            log.warning("briefing news failed: %s", e)
            problems.append(f"news: {e}")

    return "\n\n".join(blocks), commits, problems


def send(cfg, wa):
    msg, commits, problems = build(cfg)
    wa.send(msg)
    for c in commits:
        c()
    if problems:  # message went out, but tell Health something is off
        raise JobWarning("Briefing sent with missing parts:\n" + "\n".join(problems))
    return 1
