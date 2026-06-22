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
DEPLOY_EXPECT_VERSION="${DEPLOY_EXPECT_VERSION:-}"

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
ssh -p "$DEPLOY_PORT" "$DEPLOY_HOST" DEPLOY_PATH="$DEPLOY_PATH" DEPLOY_SERVICE="$DEPLOY_SERVICE" DEPLOY_IMAGE="$DEPLOY_IMAGE" DEPLOY_EXPECT_VERSION="$DEPLOY_EXPECT_VERSION" 'bash -s' <<'REMOTE'
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
    user: ""
    entrypoint: null
    command: null
EOF

COMPOSE=(docker compose -f "$COMPOSE_FILE" -f .archivebox-deploy.override.yml)

echo "[+] Pulling $DEPLOY_IMAGE..."
"${COMPOSE[@]}" pull "$DEPLOY_SERVICE"

echo "[+] Restarting $DEPLOY_SERVICE..."
"${COMPOSE[@]}" up -d "$DEPLOY_SERVICE"

if "${COMPOSE[@]}" config --services | grep -qx argo; then
    echo "[+] Ensuring argo tunnel is running..."
    "${COMPOSE[@]}" up -d argo
fi

echo "[+] Container status:"
"${COMPOSE[@]}" ps "$DEPLOY_SERVICE"
if "${COMPOSE[@]}" config --services | grep -qx argo; then
    "${COMPOSE[@]}" ps argo
fi

echo "[+] ArchiveBox version:"
VERSION_OUTPUT="$("${COMPOSE[@]}" exec -T "$DEPLOY_SERVICE" archivebox version </dev/null 2>&1 || true)"
printf '%s\n' "$VERSION_OUTPUT" | sed -n '1,40p'
if [[ -n "$DEPLOY_EXPECT_VERSION" ]] && ! grep -q "ArchiveBox v${DEPLOY_EXPECT_VERSION}" <<<"$VERSION_OUTPUT"; then
    echo "[X] Deployed container is not running ArchiveBox ${DEPLOY_EXPECT_VERSION}" >&2
    exit 1
fi

echo "[+] Health check:"
"${COMPOSE[@]}" exec -T "$DEPLOY_SERVICE" curl -fsS --max-time 10 --connect-timeout 2 -H 'Host: admin.archivebox.io' http://127.0.0.1:8000/health/ </dev/null
REMOTE

echo "[√] Demo deploy finished."
