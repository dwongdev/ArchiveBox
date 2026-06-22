#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

DEPLOY_PATH="${DEPLOY_PATH:-/opt/archivebox.demo}"
DEPLOY_SERVICE="${DEPLOY_SERVICE:-archivebox}"
DEPLOY_IMAGE="${DEPLOY_IMAGE:-archivebox/archivebox:dev}"
DEPLOY_INTERVAL="${DEPLOY_INTERVAL:-60}"
STATE_FILE="${STATE_FILE:-${DEPLOY_PATH}/.archivebox-dev-image.digest}"
OVERRIDE_FILE="${OVERRIDE_FILE:-${DEPLOY_PATH}/.archivebox-deploy.override.yml}"

cd "$DEPLOY_PATH"

if [[ -f compose.yml ]]; then
    COMPOSE_FILE=compose.yml
elif [[ -f compose.yaml ]]; then
    COMPOSE_FILE=compose.yaml
elif [[ -f docker-compose.yml ]]; then
    COMPOSE_FILE=docker-compose.yml
else
    echo "[X] No compose file found in $DEPLOY_PATH" >&2
    exit 1
fi

cat > "$OVERRIDE_FILE" <<EOF
services:
  $DEPLOY_SERVICE:
    image: $DEPLOY_IMAGE
    user: ""
    entrypoint: null
    command: null
EOF

COMPOSE=(docker compose -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE")

remote_digest() {
    docker buildx imagetools inspect "$DEPLOY_IMAGE" | awk '/^Digest:/ {print $2; exit}'
}

deploy_digest() {
    local digest="$1"

    echo "[+] Deploying ${DEPLOY_IMAGE}@${digest}"
    "${COMPOSE[@]}" pull "$DEPLOY_SERVICE"
    "${COMPOSE[@]}" up -d "$DEPLOY_SERVICE"

    if "${COMPOSE[@]}" config --services | grep -qx argo; then
        "${COMPOSE[@]}" up -d argo
    fi

    "${COMPOSE[@]}" exec -T "$DEPLOY_SERVICE" archivebox version </dev/null | sed -n '1,40p'
    "${COMPOSE[@]}" exec -T "$DEPLOY_SERVICE" curl -fsS --max-time 10 --connect-timeout 2 -H 'Host: admin.archivebox.io' http://127.0.0.1:8000/health/ </dev/null
    printf '%s\n' "$digest" > "$STATE_FILE"
}

mkdir -p "$(dirname "$STATE_FILE")"
touch "$STATE_FILE"

while :; do
    digest="$(remote_digest || true)"
    last_digest="$(cat "$STATE_FILE" 2>/dev/null || true)"

    if [[ -z "$digest" ]]; then
        echo "[!] Could not resolve ${DEPLOY_IMAGE}; retrying in ${DEPLOY_INTERVAL}s" >&2
    elif [[ "$digest" != "$last_digest" ]]; then
        deploy_digest "$digest"
    else
        echo "[=] ${DEPLOY_IMAGE} already deployed at ${digest}"
    fi

    if [[ "${WATCH_ONCE:-0}" == "1" ]]; then
        break
    fi
    sleep "$DEPLOY_INTERVAL"
done
