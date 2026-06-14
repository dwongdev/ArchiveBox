#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

ARCHIVEBOX_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="$(cd "${ARCHIVEBOX_REPO}/.." && pwd)"
PYPI_WAIT_ATTEMPTS="${PYPI_WAIT_ATTEMPTS:-90}"
DOCKER_IMAGE_REPOS="${DOCKER_IMAGE_REPOS:-archivebox/archivebox ghcr.io/archivebox/archivebox}"

cd "${WORKSPACE_DIR}"

repo_dir() {
    local repo="$1"
    printf '%s/%s\n' "${WORKSPACE_DIR}" "${repo}"
}

current_version() {
    local repo="$1"
    uv run python - "$repo" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1], "pyproject.toml").read_text()
match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
if not match:
    raise SystemExit(f"Failed to find version in {sys.argv[1]}/pyproject.toml")
print(match.group(1))
PY
}

bump_patch_to() {
    local repo="$1"
    local version="$2"
    uv run python - "$repo" "$version" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1], "pyproject.toml")
version = sys.argv[2]
text = path.read_text()
path.write_text(re.sub(r'^version = "[^"]+"$', f'version = "{version}"', text, count=1, flags=re.MULTILINE))
PY
}

next_patch_version() {
    uv run python - "$@" <<'PY'
import re
import sys

versions = sys.argv[1:]
parts = []
for version in versions:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise SystemExit(f"Expected patch version, got {version}")
    parts.append(tuple(int(part) for part in match.groups()))
major, minor, patch = max(parts)
print(f"{major}.{minor}.{patch + 1}")
PY
}

bump_archivebox_rc() {
    uv run python - "${ARCHIVEBOX_REPO}" <<'PY'
from pathlib import Path
import json
import re
import sys

repo = Path(sys.argv[1])
pyproject_path = repo / "pyproject.toml"
package_path = repo / "etc" / "package.json"
pyproject_text = pyproject_path.read_text()
match = re.search(r'^version = "(\d+)\.(\d+)\.(\d+)(?:-?rc(\d+))?"$', pyproject_text, re.MULTILINE)
if not match:
    raise SystemExit("Expected ArchiveBox version like 0.9.31rc15")

major, minor, patch, rc = match.groups()
next_version = f"{major}.{minor}.{patch}rc{int(rc or 0) + 1}"
pyproject_path.write_text(re.sub(r'^version = "[^"]+"$', f'version = "{next_version}"', pyproject_text, count=1, flags=re.MULTILINE))

package_json = json.loads(package_path.read_text())
package_json["version"] = next_version
package_path.write_text(json.dumps(package_json, indent=2) + "\n")
print(next_version)
PY
}

set_dependency_version() {
    local repo="$1"
    local package="$2"
    local version="$3"
    uv run python - "$repo" "$package" "$version" <<'PY'
from pathlib import Path
import re
import sys

repo, package, version = sys.argv[1:]
path = Path(repo, "pyproject.toml")
text = path.read_text()
updated, count = re.subn(rf'("{re.escape(package)}>=)[^"]+(")', rf'\g<1>{version}\2', text)
if count:
    path.write_text(updated)
PY
}

assert_branch() {
    local repo="$1"
    local branch="$2"
    local actual
    actual="$(git -C "$repo" branch --show-current)"
    if [[ "$actual" != "$branch" ]]; then
        echo "[X] Expected $(basename "$repo") on ${branch}, found ${actual}" >&2
        exit 1
    fi
}

build_and_prek() {
    local repo="$1"
    (
        cd "$repo"
        rm -rf dist
        uv --no-cache build --out-dir dist
        # prek auto-fixes (ruff-format, add-trailing-comma, end-of-file-fixer,
        # …) exit with status 1 whenever they modify files. Some hooks expose
        # edits that trigger later hooks on the next pass, so keep re-running
        # until the tree is stable. A real lint failure still fails every pass
        # and kills the script after a bounded number of attempts.
        for attempt in 1 2 3 4 5; do
            if uv --no-cache run prek run --all-files; then
                break
            fi
            if [[ "$attempt" -eq 5 ]]; then
                echo "[X] prek did not converge in $(basename "$repo")" >&2
                exit 1
            fi
            echo "[*] prek auto-fixed files in $(basename "$repo"); re-running to verify clean…"
        done
        rm -rf dist
        uv --no-cache build --out-dir dist
    )
}

commit_push_publish() {
    local repo="$1"
    local branch="$2"
    local package="$3"
    local version="$4"
    local tag="v${version}"

    (
        cd "$repo"
        git add -u
        while IFS= read -r path; do
            git add -- "$path"
        done < <(git ls-files --others --exclude-standard)
        if ! git diff --cached --quiet; then
            git commit -m "release: ${package} ${version}"
        else
            echo "[*] No staged changes in ${package}; reusing existing commit."
        fi
        git push origin "$branch"
        if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
            if [[ "$(git rev-list -n1 "${tag}")" != "$(git rev-parse HEAD)" ]]; then
                echo "[X] Tag ${tag} already exists but does not point at HEAD in ${package}" >&2
                exit 1
            fi
        else
            git tag -a "${tag}" -m "release: ${package} ${version}"
        fi
        git push origin "refs/tags/${tag}"
        if pypi_has_release "$package" "$version"; then
            echo "[*] ${package}==${version} is already on PyPI; skipping upload."
        else
            uv --no-cache publish --trusted-publishing always dist/*
        fi
    )
}

pypi_has_release() {
    local package="$1"
    local version="$2"

    uv run python - "$package" "$version" <<'PY'
import sys
import urllib.error
import urllib.request

package, version = sys.argv[1:]
try:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/{version}/json", timeout=10):
        raise SystemExit(0)
except urllib.error.HTTPError as err:
    raise SystemExit(1 if err.code == 404 else 2)
PY
}

wait_for_pypi() {
    local package="$1"
    local version="$2"
    local attempts=0

    until pypi_has_release "$package" "$version"
    do
        attempts=$((attempts + 1))
        if [[ "$attempts" -ge "$PYPI_WAIT_ATTEMPTS" ]]; then
            echo "[X] Timed out waiting for ${package}==${version} on PyPI" >&2
            exit 1
        fi
        sleep 10
    done
}

wait_for_docker_image() {
    local image="$1"
    local attempts=0
    local max_attempts="${DOCKER_IMAGE_WAIT_ATTEMPTS:-60}"

    until docker buildx imagetools inspect "$image" >/dev/null
    do
        attempts=$((attempts + 1))
        if [[ "$attempts" -ge "$max_attempts" ]]; then
            echo "[X] Timed out waiting for ${image}" >&2
            exit 1
        fi
        echo "[*] ${image} is not published yet; waiting..."
        sleep 30
    done
}

release_python_repo() {
    local repo_name="$1"
    local branch="$2"
    local package="$3"
    local version="$4"
    local repo
    repo="$(repo_dir "$repo_name")"

    echo "[+] Releasing ${package} ${version} from ${repo_name}:${branch}"
    assert_branch "$repo" "$branch"
    build_and_prek "$repo"
    commit_push_publish "$repo" "$branch" "$package" "$version"
    wait_for_pypi "$package" "$version"
}

ABXPKG_VERSION="${ABXPKG_VERSION:-$(next_patch_version "$(current_version "$(repo_dir abxpkg)")")}"
ABX_SHARED_VERSION="${ABX_SHARED_VERSION:-$(next_patch_version "$(current_version "$(repo_dir abx-plugins)")" "$(current_version "$(repo_dir abx-dl)")")}"

bump_patch_to "$(repo_dir abxpkg)" "$ABXPKG_VERSION"
release_python_repo abxpkg main abxpkg "$ABXPKG_VERSION"

bump_patch_to "$(repo_dir abx-plugins)" "$ABX_SHARED_VERSION"
set_dependency_version "$(repo_dir abx-plugins)" abxpkg "$ABXPKG_VERSION"
release_python_repo abx-plugins main abx-plugins "$ABX_SHARED_VERSION"

bump_patch_to "$(repo_dir abx-dl)" "$ABX_SHARED_VERSION"
set_dependency_version "$(repo_dir abx-dl)" abxpkg "$ABXPKG_VERSION"
set_dependency_version "$(repo_dir abx-dl)" abx-plugins "$ABX_SHARED_VERSION"
release_python_repo abx-dl main abx-dl "$ABX_SHARED_VERSION"

ARCHIVEBOX_VERSION="$(bump_archivebox_rc)"
set_dependency_version "$ARCHIVEBOX_REPO" abxpkg "$ABXPKG_VERSION"
set_dependency_version "$ARCHIVEBOX_REPO" abx-plugins "$ABX_SHARED_VERSION"
set_dependency_version "$ARCHIVEBOX_REPO" abx-dl "$ABX_SHARED_VERSION"

echo "[+] Releasing archivebox ${ARCHIVEBOX_VERSION} from archivebox:dev"
assert_branch "$ARCHIVEBOX_REPO" dev
build_and_prek "$ARCHIVEBOX_REPO"
commit_push_publish "$ARCHIVEBOX_REPO" dev archivebox "$ARCHIVEBOX_VERSION"

(
    cd "$ARCHIVEBOX_REPO"
    wait_for_docker_image "archivebox/abx-dl:${ABX_SHARED_VERSION}"
    ./bin/release_docker.sh dev "$ARCHIVEBOX_VERSION" "sha-$(git rev-parse --short HEAD)"
    DEPLOY_IMAGE="${DOCKER_IMAGE_REPOS%% *}:dev" DEPLOY_EXPECT_VERSION="$ARCHIVEBOX_VERSION" SKIP_DOCKER=1 ./bin/deploy_dev_demo.sh
)

echo "[√] Released abxpkg ${ABXPKG_VERSION}, abx-plugins/abx-dl ${ABX_SHARED_VERSION}, archivebox ${ARCHIVEBOX_VERSION}"
