#!/usr/bin/env bash
# Use client binaries from the exact pinned service image; no apt version drift.
set -euo pipefail
mkdir -p "/tmp/erupted-ci-bin" "$TMPDIR"
for program in pg_dump pg_restore; do
  cat > "/tmp/erupted-ci-bin/$program" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec docker exec -i "$POSTGRES_CONTAINER_ID" "$program" "\$@"
EOF
  chmod +x "/tmp/erupted-ci-bin/$program"
done
echo "/tmp/erupted-ci-bin" >> "$GITHUB_PATH"

# The supervisor keeps its own UID/network. Tests receive no runner credentials.
sudo useradd --uid 2001 --create-home --groups docker ci-tests
ancestor=$(dirname "$GITHUB_WORKSPACE")
while [[ "$ancestor" != / ]]; do
  sudo setfacl -m u:ci-tests:x "$ancestor"
  ancestor=$(dirname "$ancestor")
done
mkdir -p "$GITHUB_WORKSPACE/ci-results"
sudo setfacl -R -m u:ci-tests:rwX "$GITHUB_WORKSPACE"
sudo -u ci-tests git config --global --add safe.directory "$GITHUB_WORKSPACE"
sudo -u ci-tests test -r "$GITHUB_WORKSPACE/scripts/ci/run.py"
sudo -u ci-tests test -w "$GITHUB_WORKSPACE/ci-results"


# Preserve strict V4 server-loopback attestation in the disposable Docker DB.
# The fixed relay has no credentials, and shares only the
# service's network namespace. Both test endpoints reach the same pinned server.
docker create --name erupted-ci-postgres-loopback \
  --network "container:$POSTGRES_CONTAINER_ID" \
  python@sha256:d5b1fbbc00fd3b55620a9314222498bebf09c4bf606425bf464709ed6a79f202 \
  python /run/postgres_loopback_proxy.py
docker cp "$GITHUB_WORKSPACE/scripts/ci/postgres_loopback_proxy.py" \
  erupted-ci-postgres-loopback:/run/postgres_loopback_proxy.py
docker start erupted-ci-postgres-loopback
python - <<'READY'
import os,time
from sqlalchemy import create_engine,text
for attempt in range(40):
    engine=create_engine(os.environ['TEST_POSTGRES_ADMIN_URL'])
    try:
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT inet_server_addr()::text')) in ('127.0.0.1','127.0.0.1/32')
        break
    except Exception:
        if attempt==39:raise
        time.sleep(.25)
    finally:engine.dispose()
READY
