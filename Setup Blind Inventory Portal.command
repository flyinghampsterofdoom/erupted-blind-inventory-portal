#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

./scripts/bootstrap_and_run.sh setup-only

echo
echo "Setup complete. You can now double-click 'Start Blind Inventory Portal.command'."
read -n 1 -s -r -p "Press any key to close..."
echo
