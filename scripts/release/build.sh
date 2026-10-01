#!/bin/sh
# Shared web/cron build. No database import, migration, seed or external app call.
set -eu
python -c 'import platform; assert platform.python_version() == "3.10.13", "Release requires Python 3.10.13"'
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
python -m pip check
