# Multi-arch base: works on Pi 3B+ with 32-bit (arm/v7) or 64-bit (arm64) Raspberry Pi OS.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Africa/Nairobi \
    PULSE_CONFIG=/config/config.toml \
    PULSE_DATA=/data \
    PULSE_LOGFILE=""

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY the_pulse ./the_pulse

RUN useradd --uid 1000 --create-home pulse \
 && mkdir -p /data /config && chown pulse:pulse /data
USER pulse
VOLUME ["/data"]

# Healthy while the scheduler loop keeps writing its heartbeat (every 20s).
HEALTHCHECK --interval=2m --timeout=10s --start-period=3m \
  CMD python -c "import os,sys,time; sys.exit(time.time()-os.path.getmtime('/data/heartbeat') > 300)"

ENTRYPOINT ["python", "-m", "the_pulse"]
CMD ["run"]
