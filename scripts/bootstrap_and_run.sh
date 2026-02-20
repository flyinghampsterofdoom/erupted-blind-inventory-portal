#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${1:-run}"

log() {
  printf '[bootstrap] %s\n' "$1"
}

ensure_brew() {
  if command -v brew >/dev/null 2>&1; then
    return
  fi

  log 'Homebrew not found. Attempting automatic install...'
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi

  if ! command -v brew >/dev/null 2>&1; then
    log 'Failed to install Homebrew automatically. Please install Homebrew and rerun.'
    exit 1
  fi
}

ensure_python() {
  if command -v python3 >/dev/null 2>&1; then
    return
  fi

  ensure_brew
  log 'Installing Python 3 via Homebrew...'
  brew install python
}

ensure_postgres() {
  if command -v psql >/dev/null 2>&1 && command -v createdb >/dev/null 2>&1; then
    return
  fi

  ensure_brew
  log 'Installing PostgreSQL via Homebrew...'
  brew install postgresql@16

  if [[ -d /opt/homebrew/opt/postgresql@16/bin ]]; then
    export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
  elif [[ -d /usr/local/opt/postgresql@16/bin ]]; then
    export PATH="/usr/local/opt/postgresql@16/bin:$PATH"
  fi
}

ensure_postgres_running() {
  if command -v pg_isready >/dev/null 2>&1 && pg_isready >/dev/null 2>&1; then
    return
  fi

  if command -v brew >/dev/null 2>&1; then
    log 'Starting PostgreSQL service...'
    brew services start postgresql@16 >/dev/null 2>&1 || true
  fi

  if command -v pg_isready >/dev/null 2>&1; then
    for _ in {1..10}; do
      if pg_isready >/dev/null 2>&1; then
        return
      fi
      sleep 1
    done
  fi

  log 'PostgreSQL does not appear ready. Start it and rerun if database steps fail.'
}

ensure_env_file() {
  if [[ -f .env ]]; then
    return
  fi

  log 'Creating .env with defaults...'
  cat > .env <<EOT
DATABASE_URL=postgresql+psycopg://$USER@localhost:5432/blind_inventory
APP_SECRET_KEY=replace-this
SESSION_COOKIE_NAME=blind_inventory_session
SESSION_TTL_MINUTES=30
SESSION_COOKIE_SECURE=false
SESSION_COOKIE_SAMESITE=lax
EOT
}

set_env_database_url() {
  local new_url="$1"
  python3 - <<PY
from pathlib import Path
p = Path('.env')
lines = p.read_text().splitlines()
out = []
seen = False
for line in lines:
    if line.startswith('DATABASE_URL='):
        out.append('DATABASE_URL=${new_url}')
        seen = True
    else:
        out.append(line)
if not seen:
    out.append('DATABASE_URL=${new_url}')
p.write_text('\\n'.join(out) + '\\n')
PY
}

setup_python_env() {
  ensure_python

  if [[ ! -d .venv ]]; then
    log 'Creating virtual environment...'
    python3 -m venv .venv
  fi

  # shellcheck disable=SC1091
  source .venv/bin/activate

  log 'Installing Python dependencies...'
  python -m pip install --upgrade pip
  pip install -r requirements.txt
}

db_name_from_url() {
  local url="$1"
  local after_slash="${url##*/}"
  echo "${after_slash%%\?*}"
}

setup_database() {
  ensure_postgres

  if [[ -d /opt/homebrew/opt/postgresql@16/bin ]]; then
    export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
  elif [[ -d /usr/local/opt/postgresql@16/bin ]]; then
    export PATH="/usr/local/opt/postgresql@16/bin:$PATH"
  fi

  ensure_postgres_running

  set -a
  # shellcheck disable=SC1091
  source .env
  set +a

  local db_url="${DATABASE_URL:-postgresql+psycopg://$USER@localhost:5432/blind_inventory}"
  local db_name
  db_name="$(db_name_from_url "$db_url")"

  if [[ "$db_url" == "postgresql+psycopg://postgres:postgres@localhost:5432/"* ]]; then
    db_url="postgresql+psycopg://$USER@localhost:5432/$db_name"
    set_env_database_url "$db_url"
    export DATABASE_URL="$db_url"
    log "Updated DATABASE_URL in .env to local user '$USER'."
  fi

  log "Ensuring database '$db_name' exists..."
  createdb "$db_name" >/dev/null 2>&1 || true

  log 'Applying schema...'
  psql -d "$db_name" -f sql/schema.sql >/dev/null

  log 'Seeding example records...'
  python -m app.seed_example >/dev/null
}

start_app() {
  log 'Starting app on http://127.0.0.1:8000 ...'
  exec uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
}

main() {
  ensure_env_file
  setup_python_env
  setup_database

  if [[ "$MODE" == "setup-only" ]]; then
    log 'Setup complete.'
    exit 0
  fi

  start_app
}

main "$@"
