#!/bin/sh
# Start a persistent shared FineCode WM server at container start, so the
# workspace stays warm for the life of the container. What that costs is
# documented in docs/cli.md, "Autostarting a persistent server".
#
# FineCode itself never reads FINECODE_WM_AUTOSTART — this script is its only
# consumer, and it turns it into an explicit `--keep-alive` on the server it
# starts, because keep-alive must never be ambient.
set -eu

if [ -z "${FINECODE_WM_AUTOSTART:-}" ] || [ "${FINECODE_WM_AUTOSTART}" = "0" ]; then
    exit 0
fi

VENV_PYTHON=".venvs/dev_workspace/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "FineCode WM autostart: $VENV_PYTHON not found, skipping." >&2
    exit 0
fi

# Never fatal: the workspace is fully usable without it, because every client
# starts a server on demand anyway. Failing container start over a warm cache
# would be a worse outcome than the cold start it is trying to avoid.
if "$VENV_PYTHON" -m finecode start-wm-server --detach --keep-alive; then
    echo "FineCode WM autostart: shared server running."
else
    echo "FineCode WM autostart: no shared server reachable yet; it may still be" \
        "starting, and clients attach to it when it is." >&2
fi
