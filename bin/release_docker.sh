#!/usr/bin/env bash

### Bash Environment Setup
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
# https://www.gnu.org/software/bash/manual/html_node/The-Set-Builtin.html
# set -o xtrace
set -o errexit
set -o errtrace
set -o nounset
set -o pipefail
IFS=$' '

REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && cd .. && pwd )"
cd "$REPO_DIR"

declare -a TAG_NAMES=("$@")
BRANCH_NAME="${1:-$(git rev-parse --abbrev-ref HEAD)}"
VERSION="$(grep '^version = ' "${REPO_DIR}/pyproject.toml" | awk -F'"' '{print $2}')"
GIT_SHA=sha-"$(git rev-parse --short HEAD)"
SELECTED_PLATFORMS="${DOCKER_PLATFORMS:-${SELECTED_PLATFORMS:-linux/amd64,linux/arm64}}"

# if not already in TAG_NAMES, add GIT_SHA and BRANCH_NAME  
if ! echo "${TAG_NAMES[@]}" | grep -q "$GIT_SHA"; then
   TAG_NAMES+=("$GIT_SHA")
fi
if ! echo "${TAG_NAMES[@]}" | grep -q "$BRANCH_NAME"; then
   TAG_NAMES+=("$BRANCH_NAME")
fi
if ! echo "${TAG_NAMES[@]}" | grep -q "$VERSION"; then
   TAG_NAMES+=("$VERSION")
fi

echo "[+] Building + releasing Docker image for $SELECTED_PLATFORMS: branch=$BRANCH_NAME version=$VERSION tags=${TAG_NAMES[*]}"

declare -a FULL_TAG_NAMES
# for each tag in TAG_NAMES, add archivebox/archivebox:tag and its mirrors to FULL_TAG_NAMES
for TAG_NAME in "${TAG_NAMES[@]}"; do
    [[ "$TAG_NAME" == "" ]] && continue
    FULL_TAG_NAMES+=("-t archivebox/archivebox:$TAG_NAME")              # ArchiveBox official Docker repo
    FULL_TAG_NAMES+=("-t ghcr.io/archivebox/archivebox:$TAG_NAME")      # Github Container Repo mirror
done
echo "${FULL_TAG_NAMES[@]}"

# echo "[*] Logging in to Docker Hub & Github Container Registry"
# docker login --username=nikisweeting
# docker login ghcr.io --username=pirate

echo "[^] Uploading docker image"
mkdir -p "$HOME/.cache/docker/archivebox"

# https://docs.docker.com/build/cache/backends/
# shellcheck disable=SC2068
docker buildx build \
   --platform "$SELECTED_PLATFORMS" \
   --cache-from type=local,src="$HOME/.cache/docker/archivebox" \
   --cache-to type=local,compression=zstd,mode=min,oci-mediatypes=true,dest="$HOME/.cache/docker/archivebox" \
   --push . ${FULL_TAG_NAMES[@]}   

echo "[^] Verifying pushed Docker manifests include: $SELECTED_PLATFORMS"
for TAG_NAME in "${TAG_NAMES[@]}"; do
    [[ "$TAG_NAME" == "" ]] && continue
    MANIFEST="$(docker buildx imagetools inspect "archivebox/archivebox:$TAG_NAME")"
    for REQUIRED_PLATFORM in ${SELECTED_PLATFORMS//,/$IFS}; do
        if ! echo "$MANIFEST" | grep -q "Platform:    $REQUIRED_PLATFORM"; then
            echo "[X] archivebox/archivebox:$TAG_NAME is missing platform: $REQUIRED_PLATFORM" >&2
            echo "$MANIFEST" >&2
            exit 1
        fi
    done
done
echo "[√] Docker manifests include all requested platforms."
