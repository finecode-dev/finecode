#!/bin/sh
set -eu

sh scripts/setup-dev-workspace.sh

VENV_PYTHON=".venvs/dev_workspace/bin/python"
"$VENV_PYTHON" -m finecode run setup_system
