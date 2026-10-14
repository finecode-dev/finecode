#!/bin/sh
# Installs Node via the nvm already baked into the base image.
#
# We do NOT use the ghcr.io/devcontainers/features/node feature for this:
# the base image already ships nvm itself (no Node version installed yet)
# at /usr/local/share/nvm, owned by vscode:nvm. Running the node feature on
# top of that hits its "NVM already installed" code path, which skips the
# ownership fixup it only does for a freshly created nvm dir and calls
# `nvm install` directly. That specific command works fine under a normal
# `docker run`, but fails with "Permission denied" writing into
# /usr/local/share/nvm/.cache when run inside a BuildKit RUN layer, which
# does not reproduce ownership from the base image's read-only layer
# correctly on copy-up. Running nvm install here, in postCreateCommand
# (a normal container process, not a build layer) sidesteps the bug.
set -eu

export NVM_DIR="/usr/local/share/nvm"
export NVM_SYMLINK_CURRENT=true
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh"

umask 0002
nvm install 22
nvm alias default 22
