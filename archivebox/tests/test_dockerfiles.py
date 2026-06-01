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

_REQUIRED_DOCKER_PREINSTALL_TARGETS = {
    "archivewebpage",
    "defuddle",
    "forumdl",
    "gallerydl",
    "git",
    "istilldontcareaboutcookies",
    "liteparse",
    "mercury",
    "opendataloader",
    "papersdl",
    "parse_rss_urls",
    "readability",
    "search_backend_ripgrep",
    "search_backend_sonic",
    "ublock",
    "wget",
    "ytdlp",
}

_DOCKER_PREINSTALL_EXCLUDED_TARGETS = {
    "twocaptcha",
}


def _archivebox_install_commands(dockerfile: Path) -> list[str]:
    text = dockerfile.read_text()
    return re.findall(r"archivebox install ([^\\\n]+)", text)


def _abx_dl_plugin_install_targets(dockerfile: Path) -> set[str]:
    text = dockerfile.read_text()
    commands = re.findall(r"abx-dl plugins --install((?: \\\n|[^\n])*)", text)
    targets: set[str] = set()
    for command in commands:
        command_text = command.replace("\\", " ")
        targets.update(re.findall(r"\b[a-z][a-z0-9_]*\b", command_text))
    return targets


def test_dockerfiles_install_binaries_required_by_version_validation() -> None:
    for dockerfile_name in ("Dockerfile", "Dockerfile.multistage"):
        commands = _archivebox_install_commands(REPO_ROOT / dockerfile_name)
        target_command = max(commands, key=lambda command: len(command.split()))
        targets = set(target_command.split())
        assert _REQUIRED_DOCKER_INSTALL_TARGETS <= targets


def test_dockerfile_prewarms_stable_plugin_dependencies_without_optional_captcha() -> None:
    targets = _abx_dl_plugin_install_targets(REPO_ROOT / "Dockerfile")
    assert _REQUIRED_DOCKER_PREINSTALL_TARGETS <= targets
    assert not (_DOCKER_PREINSTALL_EXCLUDED_TARGETS & targets)
