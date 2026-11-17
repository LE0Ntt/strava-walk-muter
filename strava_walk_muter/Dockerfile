# Home Assistant Python base image (Alpine 3.24, Python 3.14, S6 overlay as /init).
# Supervisor >= 2026.04 no longer passes BUILD_FROM, so the base image is fixed here.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24

RUN pip3 install --no-cache-dir requests==2.34.2

COPY strava_walk_muter.py /app/strava_walk_muter.py

WORKDIR /data
CMD [ "python3", "-u", "/app/strava_walk_muter.py" ]
