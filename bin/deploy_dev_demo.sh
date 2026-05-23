#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

DEPLOY_HOST="${DEPLOY_HOST:-cabbage}"
DEPLOY_PORT="${DEPLOY_PORT:-44}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/archivebox.demo}"
DEPLOY_SERVICE="${DEPLOY_SERVICE:-archivebox}"
DEPLOY_IMAGE="${DEPLOY_IMAGE:-archivebox/archivebox:dev}"

VERSION="$(grep '^version = ' pyproject.toml | awk -F'"' '{print $2}')"
GIT_SHA="sha-$(git rev-parse --short HEAD)"

if [[ "$(git branch --show-current)" != "dev" ]]; then
    echo "[X] Run this from the dev branch." >&2
    exit 1
fi

if [[ -n "$(git status --short)" ]]; then
    echo "[X] Refusing to deploy with a dirty worktree. Commit or stash changes first." >&2
    git status --short >&2
    exit 1
fi

echo "[+] Pushing dev to GitHub..."
git push origin dev

if [[ "${SKIP_DOCKER:-0}" != "1" ]]; then
    echo "[+] Publishing Docker image tags: dev ${VERSION} ${GIT_SHA}"
    ./bin/release_docker.sh dev "$VERSION" "$GIT_SHA"
fi

if [[ "${SKIP_DEMO:-0}" == "1" ]]; then
    echo "[√] Skipped demo deploy."
    exit 0
fi

echo "[+] Deploying ${DEPLOY_IMAGE} on ${DEPLOY_HOST}:${DEPLOY_PATH}..."
ssh -p "$DEPLOY_PORT" "$DEPLOY_HOST" DEPLOY_PATH="$DEPLOY_PATH" DEPLOY_SERVICE="$DEPLOY_SERVICE" DEPLOY_IMAGE="$DEPLOY_IMAGE" 'bash -s' <<'REMOTE'
set -Eeuo pipefail
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

cat > .archivebox-deploy.override.yml <<EOF
services:
  $DEPLOY_SERVICE:
    image: $DEPLOY_IMAGE
EOF

COMPOSE=(docker compose -f "$COMPOSE_FILE" -f .archivebox-deploy.override.yml)

echo "[+] Pulling $DEPLOY_IMAGE..."
"${COMPOSE[@]}" pull "$DEPLOY_SERVICE"

echo "[+] Restarting $DEPLOY_SERVICE..."
"${COMPOSE[@]}" up -d "$DEPLOY_SERVICE"

echo "[+] Container status:"
"${COMPOSE[@]}" ps "$DEPLOY_SERVICE"

echo "[+] ArchiveBox version:"
"${COMPOSE[@]}" exec -T "$DEPLOY_SERVICE" archivebox version | sed -n '1,40p'

echo "[+] Health check:"
"${COMPOSE[@]}" exec -T "$DEPLOY_SERVICE" curl -fsS -H 'Host: admin.archivebox.io' http://127.0.0.1:8000/health/
REMOTE

echo "[√] Demo deploy finished."
