#!/usr/bin/env bash
# Run on the Raspberry Pi from the project folder:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "==> Python venv + dependencies"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "==> Created config.toml. Edit the [whatsapp] section, then re-run ./install.sh"
  exit 0
fi

TZNOW="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo unknown)"
if [ "$TZNOW" != "Africa/Nairobi" ]; then
  echo "!! Pi timezone is '$TZNOW'. Cron times below assume Nairobi time. Fix with:"
  echo "   sudo timedatectl set-timezone Africa/Nairobi"
fi

echo "==> Installing cron schedule"
RUN="cd $DIR && .venv/bin/python -m the_pulse"
BLOCK=$(cat <<CRON
# >>> the-pulse >>>
30 5  * * *  $RUN rain-morning >/dev/null 2>&1
0  15 * * *  $RUN rain-evening >/dev/null 2>&1
45 6  * * *  $RUN news         >/dev/null 2>&1
0  18 * * *  $RUN news         >/dev/null 2>&1
0  8-20/2 * * *  $RUN kplc     >/dev/null 2>&1
5  19 * * *  $RUN kplc-remind  >/dev/null 2>&1
# <<< the-pulse <<<
CRON
)
# also drops the block from before the rename (kenyabot)
( crontab -l 2>/dev/null | sed -e '/# >>> the-pulse >>>/,/# <<< the-pulse <<</d' -e '/# >>> kenyabot >>>/,/# <<< kenyabot <<</d'; echo "$BLOCK" ) | crontab -
crontab -l | sed -n '/>>> the-pulse/,/<<< the-pulse/p'

echo "==> Indexing current KPLC notices (sends an alert only if an upcoming outage hits your areas)"
.venv/bin/python -m the_pulse kplc || true
echo "==> Done. Test WhatsApp delivery with:  .venv/bin/python -m the_pulse test"
