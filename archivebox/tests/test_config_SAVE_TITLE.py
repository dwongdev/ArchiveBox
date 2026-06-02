from archivebox.tests.conftest import run_archivebox_cmd, cli_env

import pytest

from archivebox.core.models import Snapshot
from archivebox.tests.test_orm_helpers import use_archivebox_db
from .conftest import _find_system_browser

pytestmark = pytest.mark.django_db(transaction=True)


def _install_chrome(tmp_path, env):
    env["CHROME_ISOLATION"] = "snapshot"
    system_browser = _find_system_browser()
    if system_browser:
        env["CHROME_BINARY"] = str(system_browser)
        return

    install_process = run_archivebox_cmd(
        ["install", "chrome"],
        cwd=tmp_path,
        env=env,
        timeout=600,
    )
    assert install_process.returncode == 0, install_process.stderr or install_process.stdout


def test_title_is_extracted(tmp_path, initialized_archive):
    """Test that title is extracted from the page."""
    env = cli_env(disable_extractors=True)
    env.update({"SAVE_TITLE": "true"})
    _install_chrome(tmp_path, env)
    add_process = run_archivebox_cmd(
        ["add", "--plugins=chrome,wget,title", "https://example.com"],
        env=env,
    )
    assert add_process.returncode == 0, add_process.stderr or add_process.stdout

    with use_archivebox_db(tmp_path):
        title = Snapshot.objects.values_list("title", flat=True).get()

    assert title is not None
    assert "Example" in title


def test_title_is_listed_by_search_alias(tmp_path, initialized_archive):
    """
    https://github.com/ArchiveBox/ArchiveBox/issues/330
    Unencoded content should not be rendered as it facilitates xss injections
    and breaks the layout.
    """
    env = cli_env(disable_extractors=True)
    env.update({"SAVE_TITLE": "true"})
    _install_chrome(tmp_path, env)
    add_process = run_archivebox_cmd(
        ["add", "--plugins=chrome,wget,title", "https://example.com"],
        env=env,
    )
    assert add_process.returncode == 0, add_process.stderr or add_process.stdout
    list_process = run_archivebox_cmd(
        ["search"],
        env=env,
    )
    assert list_process.returncode == 0, list_process.stderr or list_process.stdout

    output = list_process.stdout
    assert "https://example.com" in output
