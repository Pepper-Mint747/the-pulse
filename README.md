# The Pulse

WhatsApp alerts from a Raspberry Pi, sent through an existing open-wa server:

| Job | When | What you get |
|---|---|---|
| `briefing` | 06:00 | **Morning briefing**: today's weather and commute rain, any KPLC outage today, holiday heads-up, KES rates (USD/GBP/EUR), top 5 headlines |
| `kplc` | every 2h, 08:00–20:00 | New KPLC planned outages for **home** (Wangige), **work** (Westlands) and **nearby** areas |
| `kplc-remind` | 19:05 | "Power goes off tomorrow" reminder |
| `rain-evening` | 15:00 | Re-check of the ride home (new rain, or "it cleared up"). Workdays only |
| `news` | 18:00 | Evening headlines (skips stories the briefing already sent) |
| `nse-daily` | 15:45, trading days | NSE blue-chip signals that **changed** today (stance flips, dips, big moves). Silent if nothing changed |
| `nse-weekly` | Fri 17:00 | Full NSE review: every watched stock grouped 🟢 Accumulate / ⚪ Neutral / 🔴 Wait, each with its reasons, top dividend yields and the week's best and worst |
| `rain-morning` | off | Standalone rain alert, if you want it in addition to the briefing |

**Days off:** on weekends and Kenyan public holidays (from the `holidays` package plus `[calendar] extra_holidays`), commute alerts are skipped. You get one daytime rain check for home instead (`weekend_window`).

**NSE signals** are long-term and dividend focused, and every rule is spelled out in `[nse]` in `config.toml`:
- Dividend yield at today's price: 8% or more counts as positive, under 3% as negative.
- P/E against sector peers: 20% cheaper counts as positive, 30% dearer as negative. This only applies when at least 3 peers are on the watchlist.
- Price against the moving average: 10% or more below counts as a dip (positive), 15% or more above as stretched (negative).
- A loss-making company counts as negative.
- A "falling knife" warning (down 25% or more over a year) and big daily moves are flagged, but don't change the score.

Two or more positives make a stock 🟢 Accumulate. Any negative makes it 🔴 Wait / avoid. Anything else is ⚪ Neutral.
These are signals, not financial advice. Broker fees and levies cost roughly 2% per trade.

**Failure alerts:** if a job fails, or KPLC's page or PDF format stops parsing, you get a 🚨 WhatsApp message. It comes at most once a day per job, followed by a ✅ when the job recovers.

## Run with Docker (recommended)

```bash
# on the Pi (Docker installed: curl -fsSL https://get.docker.com | sh)
rsync -av --exclude .venv --exclude data --exclude logs --exclude .env ./ pi@raspberrypi.local:~/the-pulse/
ssh pi@raspberrypi.local
cd ~/the-pulse
cp .env.example .env && nano .env          # WA_API_URL, WA_API_KEY, WA_TO
cp config.example.toml config.toml         # tweak keywords / schedule if you like
docker compose up -d --build
docker compose run --rm the-pulse test      # send a test WhatsApp message
docker compose logs -f
```

The container runs `python -m the_pulse run`, a built-in scheduler using the `[schedule]` times in `config.toml`.
After you edit `config.toml`, run `docker compose restart`.

### Notification times (env)

Set these in `.env` using 24-hour `HH:MM` Nairobi time, with commas between several times. Then run `docker compose up -d` to apply them.

| Variable | Default |
|---|---|
| `SCHEDULE_BRIEFING` | `06:00` |
| `SCHEDULE_RAIN_MORNING` | *(off)* |
| `SCHEDULE_RAIN_EVENING` | `15:00` |
| `SCHEDULE_NEWS` | `18:00` |
| `SCHEDULE_KPLC` | `08:00,10:00,12:00,14:00,16:00,18:00,20:00` |
| `SCHEDULE_KPLC_REMIND` | `19:05` |
| `SCHEDULE_NSE_DAILY` | `15:45` |
| `SCHEDULE_NSE_WEEKLY` | `fri 17:00` (weekly jobs take `day HH:MM`) |

Env vars override `[schedule]` in `config.toml`. An empty value (`SCHEDULE_NEWS=`) turns that notification off.
Check what's active with `docker compose run --rm the-pulse schedule`.
Alert state is kept in the `the-pulse-data` volume.
To trigger a job by hand, run `docker compose run --rm the-pulse news` (or `kplc`, `rain-morning`, `--dry-run`, and so on).

## Setup without Docker (cron)

```bash
rsync -av --exclude .venv --exclude data --exclude logs ./ pi@raspberrypi.local:~/the-pulse/
ssh pi@raspberrypi.local
cd ~/the-pulse
./install.sh                    # first run creates config.toml
nano config.toml                # set whatsapp.api_url, api_key, to
./install.sh                    # installs the cron jobs
.venv/bin/python -m the_pulse test
```

## Useful commands

```bash
.venv/bin/python -m the_pulse news --dry-run    # print instead of sending
tail -f logs/the-pulse.log
```

## Tuning

Everything lives in `config.toml`: outage keywords, commute hours, rain thresholds, news feeds and the ⭐ keywords.
Keyword matching ignores case and spacing, because KPLC's PDFs often split words.
Avoid short or ambiguous place names. For example, "Kibiku" also matches a Kibiku in Ngong.
State is kept in `data/` so you never get the same alert twice. Delete that folder to reset it.
