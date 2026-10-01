#!/bin/sh
# Python owns the execution gate for shell, direct-script and module invocations.
set -eu
export PYTHONPATH=.
exec python scripts/run_schedule_automation.py
