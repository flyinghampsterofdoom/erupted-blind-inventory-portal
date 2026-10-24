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
sudo setfacl -R -m u:ci-tests:rwX "$GITHUB_WORKSPACE"
sudo -u ci-tests git config --global --add safe.directory "$GITHUB_WORKSPACE"
