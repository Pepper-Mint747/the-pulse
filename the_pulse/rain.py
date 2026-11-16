"""Rain alerts for the Wangige <-> Westlands commute (Open-Meteo, no API key).

On workdays: morning + evening commute windows at home and work.
On weekends / public holidays: one daytime window at home (or nothing, if disabled).
"""
from .core import State, http, now, workday

API = "https://api.open-meteo.com/v1/forecast"


def hourly(session, loc):
    r = session.get(API, params={
        "latitude": loc["lat"], "longitude": loc["lon"],
        "hourly": "precipitation_probability,precipitation,weather_code,temperature_2m",
        "timezone": "Africa/Nairobi", "forecast_days": 1,
    }, timeout=30)
    r.raise_for_status()
    h = r.json()["hourly"]
    return [
        {"hour": int(t[11:13]), "prob": p or 0, "mm": mm or 0.0, "code": c or 0, "temp": tc}
        for t, p, mm, c, tc in zip(h["time"], h["precipitation_probability"], h["precipitation"],
                                   h["weather_code"], h["temperature_2m"])
    ]


def assess(hours, window, cfg):
    lo, hi = window
    wet = [h for h in hours if lo <= h["hour"] < hi
           and (h["prob"] >= cfg["probability_threshold"] or h["mm"] >= cfg["mm_threshold"])]
    if not wet:
        return None
    return {
        "from": min(h["hour"] for h in wet), "to": max(h["hour"] for h in wet) + 1,
        "prob": max(h["prob"] for h in wet), "mm": round(sum(h["mm"] for h in wet), 1),
        "storm": any(h["code"] >= 95 for h in wet),
    }


def line(place, w):
    icon = "⛈" if w["storm"] else "🌧"
    extra = " (thunderstorms)" if w["storm"] else ""
    return f"{icon} {place}: {w['from']:02d}:00–{w['to']:02d}:00, up to {w['prob']}% chance, ~{w['mm']}mm{extra}"


def temp_range(hours, window):
    temps = [h["temp"] for h in hours if window[0] <= h["hour"] < window[1] and h["temp"] is not None]
    return (round(min(temps)), round(max(temps))) if temps else None


def forecast(cfg, part):
    """Work out today's rain picture.

    part: 'morning' (whole day) or 'evening' (ride home only).
    Returns dict(workday, reason, sections=[(title, [lines])], evening_wet, temps).
    """
    rc, locs = cfg["rain"], cfg["locations"]
    is_work, reason = workday(cfg, now().date())
    s = http()
    out = {"workday": is_work, "reason": reason, "sections": [], "evening_wet": False, "temps": None}

    if not is_work:
        win = rc.get("weekend_window") or []
        if part != "morning" or not win:
            return out
        hours = hourly(s, locs["home"])
        out["temps"] = temp_range(hours, win)
        w = assess(hours, win, rc)
        if w:
            out["sections"].append((f"{reason}: {locs['home']['name']}", [line(locs["home"]["name"], w)]))
        return out

    fc = {k: hourly(s, locs[k]) for k in ("home", "work")}
    out["temps"] = temp_range(fc["work"], (rc["morning_window"][0], rc["evening_window"][1]))
    # Morning: leave home, arrive at work.  Evening: leave work, arrive home.
    plan = {
        "morning": [("home", rc["morning_window"]), ("work", rc["morning_window"])],
        "evening": [("work", rc["evening_window"]), ("home", rc["evening_window"])],
    }
    for p in (["morning", "evening"] if part == "morning" else ["evening"]):
        lines = [line(locs[k]["name"], w) for k, win in plan[p] if (w := assess(fc[k], win, rc))]
        if lines:
            out["evening_wet"] |= p == "evening"
            out["sections"].append(("Morning commute" if p == "morning" else "Evening commute", lines))
    return out


def day_state():
    st = State("rain")
    today = now().date().isoformat()
    if st.data.get("date") != today:
        st.data = {"date": today, "evening_warned": False}
    return st


def remember_evening(fc):
    st = day_state()
    st.data["evening_warned"] = fc["evening_wet"]
    st.save()


def render(sections):
    return "\n\n".join(f"*{title}*\n" + "\n".join(lines) for title, lines in sections)


TIP = "_Carry an umbrella and expect slow traffic on Waiyaki Way._"


def check(cfg, wa, part):
    """Standalone rain alert job (sent only when rain is likely)."""
    fc = forecast(cfg, part)
    st = day_state()
    msg = None
    if fc["sections"]:
        if part == "evening" and st.data["evening_warned"]:
            return 0  # already told you this morning
        if not fc["workday"]:
            head, tip = "☔ *Rain today*", "_Plan outdoor stuff around it._"
        else:
            head = "☔ *Rain alert*" if part == "morning" else "☔ *Rain update for your ride home*"
            tip = TIP
        msg = f"{head}\n\n{render(fc['sections'])}\n\n{tip}"
    elif part == "evening" and fc["workday"] and st.data["evening_warned"]:
        msg = "🌤 *Rain update:* the evening rain forecast has cleared, looks dry for your ride home."
    if fc["workday"]:
        remember_evening(fc)
    if msg:
        wa.send(msg)
    return 1 if msg else 0
