#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not found."
  exit 1
fi

echo "Installing context-agent-local globally as 'ctx'..."

if command -v pipx >/dev/null 2>&1; then
  pipx install "$ROOT_DIR"
  echo "Installed with pipx."
  exit 0
fi

python3 -m pip install --user "$ROOT_DIR"
echo "Installed with pip --user."
echo "If 'ctx' is not found, ensure your user bin directory is on PATH and restart your terminal."

