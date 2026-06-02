import os
from pathlib import Path

import pytest

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.conftest import run_archivebox_cmd_cwd
from archivebox.tests.test_orm_helpers import use_archivebox_db
from .conftest import build_test_env, get_free_port, init_archive

pytestmark = pytest.mark.django_db(transaction=True)


def _assert_command_ok(command: str, stdout: str, stderr: str, code: int) -> None:
    assert code == 0, f"{command} failed with code {code}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"


def _latest_crawl_id(cwd: Path) -> str:
    with use_archivebox_db(cwd):
        crawl_id = Crawl.objects.order_by("-created_at").values_list("id", flat=True).first()
        assert crawl_id is not None
        return str(crawl_id)


def _crawl_state(cwd: Path, crawl_id: str) -> dict[str, object]:
    with use_archivebox_db(cwd):
        crawl = Crawl.objects.select_related("created_by").get(id=crawl_id)
        snapshots = list(
            Snapshot.objects.filter(crawl=crawl)
            .order_by("depth", "url")
            .values("id", "url", "depth", "status", "parent_snapshot_id", "downloaded_at"),
        )
        results = list(
            ArchiveResult.objects.filter(snapshot__crawl=crawl)
            .order_by("snapshot__url", "plugin", "hook_name")
            .values("snapshot__url", "plugin", "hook_name", "status", "output_files", "output_size"),
        )
        return {
            "status": crawl.status,
            "retry_at": crawl.retry_at,
            "urls": crawl.urls,
            "config": crawl.config or {},
            "output_dir": Path(crawl.output_dir),
            "snapshots": snapshots,
            "results": results,
        }


@pytest.mark.timeout(240)
def test_crawl_service_run_processes_queued_crawl_and_applies_crawl_config(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(
        port,
        PLUGINS="wget,parse_html_urls",
        SAVE_WGET="True",
        SAVE_FAVICON="False",
        SAVE_TITLE="False",
    )
    root_url = recursive_test_site["root_url"]
    about_url = recursive_test_site["child_urls"][0]
    contact_url = recursive_test_site["child_urls"][2]

    add_stdout, add_stderr, add_code = run_archivebox_cmd_cwd(
        [
            "add",
            "--bg",
            "--depth=0",
            "--max-urls=20",
            "--plugins=wget,parse_html_urls",
            "--tag=crawl-service-e2e",
            "--url-denylist=/contact$",
            root_url,
            about_url,
            contact_url,
        ],
        cwd=tmp_path,
        env=env,
        timeout=120,
    )
    _assert_command_ok("archivebox add --bg", add_stdout, add_stderr, add_code)

    crawl_id = _latest_crawl_id(tmp_path)
    queued_state = _crawl_state(tmp_path, crawl_id)
    assert queued_state["status"] == Crawl.StatusChoices.QUEUED
    assert queued_state["retry_at"] is not None
    assert queued_state["config"]["PLUGINS"] == "wget,parse_html_urls"
    assert queued_state["config"]["URL_DENYLIST"] == "/contact$"
    assert queued_state["snapshots"] == []

    run_stdout, run_stderr, run_code = run_archivebox_cmd_cwd(
        ["run", "--crawl-id", crawl_id],
        cwd=tmp_path,
        env=env,
        timeout=240,
    )
    _assert_command_ok("archivebox run --crawl-id", run_stdout, run_stderr, run_code)

    state = _crawl_state(tmp_path, crawl_id)
    snapshots = state["snapshots"]
    results = state["results"]
    snapshotted_urls = {row["url"] for row in snapshots}

    assert state["status"] == Crawl.StatusChoices.SEALED
    assert state["retry_at"] is None
    assert snapshotted_urls == {root_url, about_url}
    assert contact_url not in snapshotted_urls
    assert {row["depth"] for row in snapshots} == {0}
    assert all(row["status"] == Snapshot.StatusChoices.SEALED for row in snapshots)
    assert all(row["downloaded_at"] is not None for row in snapshots)
    assert all("/contact" not in row["url"] for row in snapshots)
    assert all(row["parent_snapshot_id"] is None for row in snapshots)

    result_statuses = {(row["plugin"], row["status"]) for row in results}
    assert ("wget", ArchiveResult.StatusChoices.SUCCEEDED) in result_statuses
    assert any(row["plugin"].endswith("parse_html_urls") and row["status"] == ArchiveResult.StatusChoices.SUCCEEDED for row in results)
    assert any(row["plugin"] == "wget" and row["output_size"] > 0 for row in results)

    assert list((tmp_path / "archive/users/system/snapshots").rglob("wget/**/*.html"))
    assert list((tmp_path / "archive/users/system/snapshots").rglob("parse_html_urls/**/urls.jsonl"))
