#!/usr/bin/env bash
# Use client binaries from the exact pinned service image; no apt version drift.
set -euo pipefail
mkdir -p "$RUNNER_TEMP/ci-bin" "$TMPDIR"
for program in pg_dump pg_restore; do
  cat > "$RUNNER_TEMP/ci-bin/$program" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec docker exec -i "$POSTGRES_CONTAINER_ID" "$program" "\$@"
EOF
  chmod +x "$RUNNER_TEMP/ci-bin/$program"
done
echo "$RUNNER_TEMP/ci-bin" >> "$GITHUB_PATH"
