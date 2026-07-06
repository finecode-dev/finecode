#!/bin/sh
# Creates (or validates) the dev_workspace venv with all monorepo packages installed
# editable from local source. Shared by the devcontainer and CI so the two setup paths
# cannot drift apart — see docs/guides/developing-finecode.md#continuous-integration.
#
# Must be run from the repo root.
set -eu

VENV_DIR=".venvs/dev_workspace"

# venv layout differs on Windows (python.exe under Scripts/, not bin/); this script also
# runs under Git Bash on GitHub Actions' windows-* runners, where `uname -s` reports
# MINGW/MSYS/CYGWIN rather than a POSIX name.
case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) VENV_PYTHON="$VENV_DIR/Scripts/python.exe" ;;
    *) VENV_PYTHON="$VENV_DIR/bin/python" ;;
esac

is_valid_venv() {
    if [ ! -x "$VENV_PYTHON" ]; then
        return 1
    fi

    # Requested check first; fall back to a guaranteed CLI entrypoint check.
    if "$VENV_PYTHON" -m finecode version >/dev/null 2>&1; then
        return 0
    fi

    if "$VENV_PYTHON" -m finecode version >/dev/null 2>&1; then
        return 0
    fi

    "$VENV_PYTHON" -m finecode --help >/dev/null 2>&1
}

recreate_venv() {
    rm -rf "$VENV_DIR"
    python -m venv "$VENV_DIR"

    # Ensure expected uv version is present in pipx-managed tools.
    pipx install --force "uv==0.11.*"

    # bootstrap cannot be used here because finecode has to be installed from local sources.
    # The editable package list is generated (not hand-maintained) so it can't go stale —
    # see list_dev_workspace_editables.py and docs/guides/developing-finecode.md#continuous-integration.
    EDITABLE_ARGS=$(python scripts/list_dev_workspace_editables.py)
    uv pip install --python "$VENV_PYTHON" --group dev_workspace $EDITABLE_ARGS -e .

    "$VENV_PYTHON" -m finecode prepare-envs
}

if [ -d "$VENV_DIR" ] && is_valid_venv; then
    echo "dev_workspace venv is valid; skipping recreation."
else
    recreate_venv
fi
