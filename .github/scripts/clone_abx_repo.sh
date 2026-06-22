#!/usr/bin/env bash
set -Eeuo pipefail

repo_name="$1"
target_dir="${2:-$repo_name}"

version="$(
python3 - "$repo_name" <<'PY'
import re
import sys
from pathlib import Path

repo_name = sys.argv[1]
lock_text = Path("uv.lock").read_text()
match = re.search(
    rf'^\[\[package\]\]\s*\nname = "{re.escape(repo_name)}"\s*\nversion = "([^"]+)"',
    lock_text,
    re.MULTILINE,
)
if not match:
    raise SystemExit(f"Could not find {repo_name} in uv.lock")
print(match.group(1))
PY
)"

echo "Cloning ArchiveBox/${repo_name}@v${version} into ${target_dir}"
git clone --depth=1 --branch "v${version}" "https://github.com/ArchiveBox/${repo_name}.git" "$target_dir"
