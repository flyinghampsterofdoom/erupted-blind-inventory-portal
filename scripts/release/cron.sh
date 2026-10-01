#!/bin/sh
# Configure this same command for web-candidate cron and recovery cron builds.
# The gate prevents database work during resume/build/rollback transitions.
set -eu
if [ "${SCHEDULE_AUTOMATION_EXECUTION_ENABLED:-false}" != "true" ]; then
    echo 'Schedule automation execution disabled.'
    exit 0
fi
export PYTHONPATH=.
python -c 'from app.schema_contract import assert_supported_schema; assert_supported_schema(); from scripts.run_schedule_automation import main; main()'
