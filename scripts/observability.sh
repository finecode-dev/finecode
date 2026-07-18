#!/bin/sh
# Convenience wrapper around the local observability stack (docker-compose.otel.yml).
#
# The stack is gated behind the "otel" Compose profile and stays down by default.
# This script is the on-demand toggle: bring it up/down without editing .env or
# reopening the devcontainer.
#
# Run this on the HOST (where the Docker daemon is reachable) from the repo root —
# the devcontainer does not mount the Docker socket. Services attach to the shared,
# externally-named "finecode-net" network, so the workspace container reaches the
# collector at http://otel-lgtm:4317 regardless of which side started it.
#
# For a persistent setup that comes up with the devcontainer instead, set
# COMPOSE_PROFILES=otel in .env (see .env.example).
#
# Usage: scripts/observability.sh {up|down|status|logs}
set -eu

COMPOSE_FILE="docker-compose.otel.yml"
PROFILE="otel"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "error: $COMPOSE_FILE not found — run this from the repo root." >&2
    exit 1
fi

compose() {
    docker compose -f "$COMPOSE_FILE" --profile "$PROFILE" "$@"
}

case "${1:-}" in
    up)
        compose up -d
        echo "Observability stack is up. Grafana: http://localhost:3000  Jaeger: http://localhost:16686"
        ;;
    down)
        # `down` ignores profiles for removal, so pass the profile explicitly to
        # ensure the gated services are torn down too.
        compose down
        ;;
    status)
        compose ps
        ;;
    logs)
        shift
        compose logs -f "$@"
        ;;
    *)
        echo "Usage: scripts/observability.sh {up|down|status|logs}" >&2
        exit 2
        ;;
esac
