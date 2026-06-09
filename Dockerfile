FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Mount a volume at /data so state, the Google token, and the scanner cursor
# survive redeploys.
ENV DATA_DIR=/data
ENV PORT=8080

# One worker on purpose: the app is single-user and the scanner thread must
# not run in multiple processes.
CMD exec gunicorn --workers 1 --threads 4 --bind 0.0.0.0:${PORT} "kin_calendar.webapp:create_app()"
