#!/usr/bin/env bash
# Redeploy DataTraceMind on the EC2 host from the uploaded build archive.
#
#   ~/dtm_build/dtm-backend.tar.gz   the build, copied here with scp
#   ~/dtm_build/.env                 settings, copied into the app on every run
#   ~/data_trace_mind/               the unpacked app, replaced on every run
#
# PostgreSQL runs in Docker and keeps its data in a named volume, so the
# database survives a redeploy. The API runs in the background, writes to
# ~/dtm-service.log, and records its pid in ~/dtm-service.pid.
set -euo pipefail

BUILD_DIR="$HOME/dtm_build"
APP_DIR="$HOME/data_trace_mind"
TARBALL="$BUILD_DIR/dtm-backend.tar.gz"
LOG_FILE="$HOME/dtm-service.log"
PID_FILE="$HOME/dtm-service.pid"
export PATH="$HOME/.local/bin:$PATH"

# Everything is checked before the running service is touched.
if [[ ! -f "$TARBALL" ]]; then
  echo "No build found at $TARBALL. Copy dtm-backend.tar.gz there first." >&2
  exit 1
fi
if ! CONTENTS="$(tar -tzf "$TARBALL" 2>/dev/null)"; then
  echo "$TARBALL is not a readable .tar.gz archive." >&2
  exit 1
fi
for required_file in datatracemind/api/app.py etl/pipeline.py requirements.txt docker-compose.yml; do
  if ! grep -qxF "$required_file" <<<"$CONTENTS"; then
    echo "The archive is missing $required_file. Rebuild it from the project root." >&2
    exit 1
  fi
done
if [[ ! -f "$BUILD_DIR/.env" ]]; then
  echo "No settings found at $BUILD_DIR/.env. Copy your .env there first." >&2
  exit 1
fi

# DTM_API_HOST and DTM_API_PORT may be set in that .env file. A value in the
# shell environment takes precedence over it.
env_setting() {
  local name="$1" default="$2" value
  if [[ -n "${!name:-}" ]]; then
    printf '%s\n' "${!name}"
    return
  fi
  value="$(sed -n "/^[[:space:]]*$name[[:space:]]*=/{s/^[^=]*=[[:space:]]*//;s/[[:space:]]*\$//;p;}" \
    "$BUILD_DIR/.env" | tail -n 1)"
  value="${value#[\"\']}"
  value="${value%[\"\']}"
  printf '%s\n' "${value:-$default}"
}
HOST="$(env_setting DTM_API_HOST 127.0.0.1)"
PORT="$(env_setting DTM_API_PORT 8000)"
PROBE_HOST="$HOST"
if [[ "$HOST" == "0.0.0.0" ]]; then
  PROBE_HOST="127.0.0.1"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is missing. On Amazon Linux 2023: sudo dnf install -y docker" >&2
  exit 1
fi

# Amazon Linux uses systemd to manage the Docker daemon.
if command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet docker; then
  echo "Starting the Docker daemon..."
  sudo systemctl start docker
fi

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
  else
    echo "Cannot reach the Docker daemon. Check its status and Docker permissions." >&2
    exit 1
  fi
fi
if ! "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is missing. Install it so 'docker compose version' succeeds." >&2
  exit 1
fi

# Only the API this script started last is stopped. Its command line is
# checked as well, in case the pid now belongs to another process.
OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ "$OLD_PID" =~ ^[0-9]+$ ]] \
  && ps -p "$OLD_PID" -o args= 2>/dev/null | grep -qF "uvicorn datatracemind.api.app:app"; then
  echo "Stopping the running API (pid $OLD_PID)..."
  kill "$OLD_PID" 2>/dev/null || true
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if ! kill -0 "$OLD_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  kill -9 "$OLD_PID" 2>/dev/null || true
fi
rm -f "$PID_FILE"
if curl -s -o /dev/null --max-time 3 "http://$PROBE_HOST:$PORT/"; then
  echo "Port $PORT is already in use by something this script did not start. Stop it first." >&2
  exit 1
fi

# .venv is kept so that a redeploy does not download every package again.
echo "Replacing the contents of $APP_DIR..."
mkdir -p "$APP_DIR"
find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
tar -xzf "$TARBALL" -C "$APP_DIR"
install -m 600 "$BUILD_DIR/.env" "$APP_DIR/.env"
cd "$APP_DIR"

echo "Installing Python packages..."
if command -v uv >/dev/null 2>&1; then
  if [[ ! -x .venv/bin/python ]]; then
    uv venv --python 3.13 .venv
  fi
  uv pip install --quiet --python .venv/bin/python -r requirements.txt
else
  if [[ ! -x .venv/bin/python ]]; then
    "${DTM_PYTHON:-python3}" -m venv .venv
  fi
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi

echo "Starting PostgreSQL..."
if ! "${DOCKER[@]}" compose up -d --wait postgres; then
  echo "PostgreSQL did not become ready. Container logs:" >&2
  "${DOCKER[@]}" compose logs --tail=50 postgres >&2
  exit 1
fi

echo "Starting the API on $HOST:$PORT..."
echo "--- $(date '+%Y-%m-%d %H:%M:%S') start ---" >>"$LOG_FILE"
nohup "$APP_DIR/.venv/bin/python" -m uvicorn datatracemind.api.app:app \
  --host "$HOST" --port "$PORT" --workers 1 >>"$LOG_FILE" 2>&1 </dev/null &
API_PID=$!
echo "$API_PID" >"$PID_FILE"

for ((attempt = 1; attempt <= 60; attempt++)); do
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "The API stopped during startup. Last log lines:" >&2
    tail -n 40 "$LOG_FILE" >&2
    exit 1
  fi
  if curl -fs -o /dev/null --max-time 3 "http://$PROBE_HOST:$PORT/api/health"; then
    echo "DataTraceMind is running on $HOST:$PORT (pid $API_PID). Log: $LOG_FILE"
    exit 0
  fi
  sleep 1
done
echo "The API did not answer within 60 seconds. Last log lines:" >&2
tail -n 40 "$LOG_FILE" >&2
exit 1
