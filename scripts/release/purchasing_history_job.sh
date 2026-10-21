#!/bin/sh
# One entrypoint for hosted jobs, build preflight, and isolated rehearsal.
set -eu
release_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$release_root"
exec python -m scripts.release.refresh_purchasing_history "$@"
