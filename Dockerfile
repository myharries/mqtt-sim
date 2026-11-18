FROM python:3.12-alpine

LABEL org.opencontainers.image.title="mqtt-sim" \
      org.opencontainers.image.description="CSV-driven MQTT simulator: publishes test values (and an optional GPS route) to an MQTT broker" \
      org.opencontainers.image.source="https://github.com/myharries/mqtt-sim" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/myharries/mqtt-sim/main/icon.png?v=2"

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PROFILE=profiles/van-victron.csv

WORKDIR /app
COPY mqtt_sim.py ./
COPY lib/ ./lib/
COPY profiles/ ./profiles/
COPY routes/ ./routes/

VOLUME ["/data"]
CMD ["python", "-u", "/app/mqtt_sim.py"]
