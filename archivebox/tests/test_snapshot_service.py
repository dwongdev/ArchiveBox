import json
import os
import time
from pathlib import Path

import pytest
import requests

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.tests.conftest import run_archivebox_cmd_cwd
from archivebox.tests.test_orm_helpers import use_archivebox_db
from archivebox.workers.models import RETRY_AT_MAX
from .conftest import (
    build_test_env,
    create_admin_and_token,
    get_crawl_runtime_state,
    get_free_port,
    init_archive,
    start_server,
    stop_server,
    wait_for_http,
    wait_for_snapshot_capture,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _assert_command_ok(command: str, stdout: str, stderr: str, code: int) -> None:
    assert code == 0, f"{command} failed with code {code}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"


def _snapshot_state(cwd: Path, url: str) -> dict[str, object]:
    with use_archivebox_db(cwd):
        snapshot = Snapshot.objects.select_related("crawl", "crawl__created_by").get(url=url)
        snapshot_dir = Path(snapshot.output_dir)
        crawl_dir = Path(snapshot.crawl.output_dir)
        crawl_link = crawl_dir / "snapshots" / Snapshot.extract_domain_from_url(snapshot.url) / str(snapshot.id)
        results = list(
            ArchiveResult.objects.filter(snapshot=snapshot)
            .order_by("plugin", "hook_name")
            .values("plugin", "hook_name", "status", "output_files", "output_size"),
        )
        return {
            "id": str(snapshot.id),
            "crawl_id": str(snapshot.crawl_id),
            "status": snapshot.status,
            "retry_at": snapshot.retry_at,
            "downloaded_at": snapshot.downloaded_at,
            "output_size": snapshot.output_size,
            "snapshot_dir": snapshot_dir,
            "crawl_dir": crawl_dir,
            "crawl_link": crawl_link,
            "results": results,
        }


def _paused_snapshot_state(cwd: Path, snapshot_id: str) -> dict[str, object]:
    with use_archivebox_db(cwd):
        snapshot = Snapshot.objects.select_related("crawl").get(id=snapshot_id)
        succeeded_results = ArchiveResult.objects.filter(snapshot=snapshot, status=ArchiveResult.StatusChoices.SUCCEEDED).count()
        return {
            "status": snapshot.status,
            "retry_at": snapshot.retry_at,
            "crawl_status": snapshot.crawl.status,
            "succeeded_results": succeeded_results,
            "snapshot_dir": Path(snapshot.output_dir),
        }


def _wait_for_paused_scheduler_marker(cwd: Path, snapshot_id: str, timeout: int = 60) -> dict[str, object]:
    deadline = time.time() + timeout
    last_state: dict[str, object] = {}
    while time.time() < deadline:
        last_state = _paused_snapshot_state(cwd, snapshot_id)
        if last_state["status"] == Snapshot.StatusChoices.PAUSED and last_state["retry_at"] == RETRY_AT_MAX:
            return last_state
        if last_state["status"] == Snapshot.StatusChoices.SEALED:
            return last_state
        time.sleep(1)
    raise AssertionError(f"paused snapshot did not settle back to retry_at=MAX: {last_state}")


def _wait_for_crawl_snapshot_rows(cwd: Path, crawl_id: str, timeout: int = 45) -> dict[str, object]:
    deadline = time.time() + timeout
    latest_state: dict[str, object] | None = None
    while time.time() < deadline:
        latest_state = get_crawl_runtime_state(cwd, crawl_id)
        if latest_state["snapshots"]:
            return latest_state
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for snapshot rows for crawl {crawl_id}: {latest_state}")


@pytest.mark.timeout(180)
def test_snapshot_service_cli_add_seals_snapshot_and_writes_indexes(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(port, PLUGINS="wget", SAVE_WGET="True")
    stdout, stderr, code = run_archivebox_cmd_cwd(
        ["add", "--depth=0", "--plugins=wget", recursive_test_site["root_url"]],
        cwd=tmp_path,
        env=env,
        timeout=180,
    )
    _assert_command_ok("archivebox add", stdout, stderr, code)

    state = _snapshot_state(tmp_path, recursive_test_site["root_url"])
    snapshot_dir = state["snapshot_dir"]
    crawl_link = state["crawl_link"]
    assert isinstance(snapshot_dir, Path)
    assert isinstance(crawl_link, Path)

    assert state["status"] == Snapshot.StatusChoices.SEALED
    assert state["retry_at"] is None
    assert state["downloaded_at"] is not None
    assert snapshot_dir.is_dir()
    assert crawl_link.is_symlink()
    assert crawl_link.resolve() == snapshot_dir.resolve()

    index_jsonl = snapshot_dir / "index.jsonl"
    assert index_jsonl.is_file()

    records = [json.loads(line) for line in index_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records[0]["type"] == "Snapshot"
    assert records[0]["id"] == state["id"]
    assert any(record.get("type") == "ArchiveResult" and record.get("plugin") == "wget" for record in records)

    wget_files = [path for path in (snapshot_dir / "wget").rglob("*") if path.is_file()]
    assert wget_files
    assert any("Root" in path.read_text(encoding="utf-8", errors="ignore") for path in wget_files if path.suffix in (".html", ".txt"))
    assert any(result["plugin"] == "wget" and result["status"] == ArchiveResult.StatusChoices.SUCCEEDED for result in state["results"])


@pytest.mark.timeout(240)
def test_paused_snapshot_survives_server_restart_and_resumes_via_api(tmp_path, recursive_test_site):
    os.chdir(tmp_path)
    init_archive(tmp_path)

    port = get_free_port()
    env = build_test_env(port, PLUGINS="wget", SAVE_WGET="True")
    api_token = create_admin_and_token(tmp_path)
    api_headers = {
        "Host": f"api.archivebox.localhost:{port}",
        "X-ArchiveBox-API-Key": api_token,
    }

    try:
        start_server(tmp_path, env=env, port=port)
        wait_for_http(port, host=f"api.archivebox.localhost:{port}", path="/api/v1/docs")

        crawl_response = requests.post(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawls",
            headers=api_headers,
            json={
                "urls": [recursive_test_site["root_url"]],
                "max_depth": 0,
                "tags": ["snapshot-pause-restart-e2e"],
                "config": {"PLUGINS": "wget", "URL_ALLOWLIST": r"127\.0\.0\.1[:/].*"},
            },
            timeout=10,
        )
        assert crawl_response.status_code == 200, crawl_response.text
        crawl_id = crawl_response.json()["id"]
        crawl_state = _wait_for_crawl_snapshot_rows(tmp_path, crawl_id)
        snapshot_id = crawl_state["snapshots"][0]["id"]

        pause_response = requests.patch(
            f"http://127.0.0.1:{port}/api/v1/crawls/crawl/{crawl_id}",
            headers=api_headers,
            json={"action": "pause"},
            timeout=10,
        )
        assert pause_response.status_code == 200, pause_response.text

        current_state = _paused_snapshot_state(tmp_path, snapshot_id)
        if current_state["status"] == Snapshot.StatusChoices.SEALED:
            assert current_state["succeeded_results"] > 0
            return

        paused_state = _wait_for_paused_scheduler_marker(tmp_path, snapshot_id)
        if paused_state["status"] == Snapshot.StatusChoices.SEALED:
            assert paused_state["succeeded_results"] > 0
            return
        assert paused_state["succeeded_results"] == 0
        assert not list((paused_state["snapshot_dir"] / "wget").rglob("*.html"))

        stop_server(tmp_path)
        start_server(tmp_path, env=env, port=port)
        wait_for_http(port, host=f"api.archivebox.localhost:{port}", path="/api/v1/docs")

        restarted_state = _wait_for_paused_scheduler_marker(tmp_path, snapshot_id)
        assert restarted_state["status"] == Snapshot.StatusChoices.PAUSED
        assert restarted_state["succeeded_results"] == 0

        resume_response = requests.patch(
            f"http://127.0.0.1:{port}/api/v1/core/snapshot/{snapshot_id}",
            headers=api_headers,
            json={"action": "resume"},
            timeout=10,
        )
        assert resume_response.status_code == 200, resume_response.text
        assert resume_response.json()["status"] == Snapshot.StatusChoices.QUEUED

        captured_text = wait_for_snapshot_capture(tmp_path, recursive_test_site["root_url"], timeout=180)
        assert "Root" in captured_text
        assert "About" in captured_text

        final_state = _snapshot_state(tmp_path, recursive_test_site["root_url"])
        assert final_state["status"] == Snapshot.StatusChoices.SEALED
        assert final_state["downloaded_at"] is not None
        assert any(
            result["plugin"] == "wget" and result["status"] == ArchiveResult.StatusChoices.SUCCEEDED for result in final_state["results"]
        )
    finally:
        stop_server(tmp_path)
