from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_DOCKER_INSTALL_TARGETS = {
    "archivewebpage",
    "defuddle",
    "forumdl",
    "gallerydl",
    "git",
    "istilldontcareaboutcookies",
    "liteparse",
    "mercury",
    "papersdl",
    "parse_rss_urls",
    "readability",
    "search_backend_ripgrep",
    "search_backend_sonic",
}


def _archivebox_install_commands(dockerfile: Path) -> list[str]:
    text = dockerfile.read_text()
    return re.findall(r"archivebox install ([^\\\n]+)", text)


def test_dockerfiles_install_binaries_required_by_version_validation() -> None:
    for dockerfile_name in ("Dockerfile", "Dockerfile.multistage"):
        commands = _archivebox_install_commands(REPO_ROOT / dockerfile_name)
        target_command = max(commands, key=lambda command: len(command.split()))
        targets = set(target_command.split())
        assert _REQUIRED_DOCKER_INSTALL_TARGETS <= targets
