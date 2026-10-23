#!/bin/sh
# Shared web/cron build. No database import, migration, seed or external app call.
set -eu
python scripts/release/workspace_evidence.py before-dependencies
python -c 'import platform; assert platform.python_version() == "3.10.13", "Release requires Python 3.10.13"'
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
python -m pip check
python scripts/release/workspace_evidence.py after-dependencies
python scripts/release/build_identity.py
sh scripts/release/purchasing_history_job.sh --preflight

# Optional disabled-by-default forecasting asset; existing cron command is unchanged.
if [ "${FORECAST_QUALIFICATION_BUILD_TOOLS:-false}" = "true" ]; then
  python scripts/release/prepare_forecast_tools.py
fi
