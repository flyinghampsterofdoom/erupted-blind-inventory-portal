#!/usr/bin/env bash
# During test execution only: new network connections can reach loopback/CI DB.
# Always remove our private chains before artifact upload, including on failure.
set -euo pipefail
cleanup() {
  sudo iptables -D OUTPUT -m owner --uid-owner 2001 -j ERUPTED_CI 2>/dev/null || true
  sudo iptables -F ERUPTED_CI 2>/dev/null || true
  sudo iptables -X ERUPTED_CI 2>/dev/null || true
  sudo ip6tables -D OUTPUT -m owner --uid-owner 2001 -j ERUPTED_CI 2>/dev/null || true
  sudo ip6tables -F ERUPTED_CI 2>/dev/null || true
  sudo ip6tables -X ERUPTED_CI 2>/dev/null || true
}
trap cleanup EXIT
sudo iptables -N ERUPTED_CI
sudo iptables -A ERUPTED_CI -o lo -j RETURN
while read -r address; do
  if [[ -n "$address" ]]; then
    sudo iptables -A ERUPTED_CI -d "$address" -p tcp -m multiport --dports 5432,5433 -j RETURN
  fi
done < <(docker inspect --format '{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}' "$POSTGRES_CONTAINER_ID")
sudo iptables -A ERUPTED_CI -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
sudo iptables -A ERUPTED_CI -j REJECT
sudo iptables -I OUTPUT 1 -m owner --uid-owner 2001 -j ERUPTED_CI
sudo ip6tables -N ERUPTED_CI
sudo ip6tables -A ERUPTED_CI -o lo -j RETURN
sudo ip6tables -A ERUPTED_CI -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
sudo ip6tables -A ERUPTED_CI -j REJECT
sudo ip6tables -I OUTPUT 1 -m owner --uid-owner 2001 -j ERUPTED_CI
sudo -u ci-tests "$(command -v python)" - <<'PY'
import socket, json
try:
    socket.create_connection(('1.1.1.1', 443), timeout=2)
except OSError:
    print(json.dumps({'external_network': 'BLOCKED', 'scope': 'test phase; dependencies installed beforehand'}))
else:
    raise RuntimeError('CI external network fence did not block a new connection')
PY
# Explicit allowlist: no supervisor token or workstation environment is inherited.
sudo -u ci-tests env -i \
  HOME=/home/ci-tests PATH="$PATH" PYTHONPATH=.:tests PYTHONDONTWRITEBYTECODE=1 \
  ENVIRONMENT=test TMPDIR=/tmp \
  DATABASE_URL="$DATABASE_URL" TEST_POSTGRES_ADMIN_URL="$TEST_POSTGRES_ADMIN_URL" \
  P1_TEST_DATABASE_URL="$P1_TEST_DATABASE_URL" SNAPSHOT_PROVIDER=mock \
  SCHEDULE_AUTOMATION_EXECUTION_ENABLED=false FORECAST_VALIDATION_WORKER_ENABLED=false \
  FORECAST_VALIDATION_SCHEDULE_ENABLED=false SQUARE_ACCESS_TOKEN= \
  "$@"
