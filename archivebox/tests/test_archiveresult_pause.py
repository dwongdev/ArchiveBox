import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.utils import timezone

from archivebox.core.models import ArchiveResult, Snapshot
from archivebox.crawls.models import Crawl
from archivebox.tests.test_orm_helpers import use_archivebox_db
from archivebox.workers.models import RETRY_AT_MAX

from .conftest import build_test_env, create_admin_and_token, get_free_port, init_archive

pytestmark = pytest.mark.django_db(transaction=True)

API_HOST = "api.archivebox.localhost:8000"


def _api_headers(token: str) -> dict[str, str]:
    return {
        "HTTP_HOST": API_HOST,
        "HTTP_X_ARCHIVEBOX_API_KEY": token,
    }


def _json_response(response):
    return json.loads(response.content.decode())


def _post_json(client, path: str, token: str, payload: dict):
    return client.post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        **_api_headers(token),
    )


def _patch_json(client, path: str, token: str, payload: dict):
    return client.patch(
        path,
        data=json.dumps(payload),
        content_type="application/json",
        **_api_headers(token),
    )


def _seed_archiveresult(
    snapshot: Snapshot,
    *,
    plugin: str,
    hook_name: str,
    status: str,
    output_text: str = "",
    output_path: str | None = None,
) -> ArchiveResult:
    output_files = {}
    output_size = 0
    output_mimetypes = ""
    if output_path is not None:
        output_bytes = output_text.encode()
        absolute_path = Path(snapshot.output_dir) / output_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(output_bytes)
        output_size = len(output_bytes)
        output_mimetypes = "text/plain"
        output_files[output_path] = {
            "extension": Path(output_path).suffix.lstrip("."),
            "mimetype": "text/plain",
            "size": output_size,
        }

    now = timezone.now()
    return ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin=plugin,
        hook_name=hook_name,
        status=status,
        output_str=output_path or output_text,
        output_files=output_files,
        output_size=output_size,
        output_mimetypes=output_mimetypes,
        start_ts=now if status != ArchiveResult.StatusChoices.QUEUED else None,
        end_ts=now if status in ArchiveResult.FINAL_STATES else None,
    )


def _snapshot_hook_name(plugin_name: str) -> str:
    from abx_dl.models import discover_plugins

    plugin = discover_plugins().get(plugin_name)
    assert plugin is not None, f"missing test plugin {plugin_name}"
    hooks = plugin.filter_hooks("Snapshot")
    assert hooks, f"missing Snapshot hooks for {plugin_name}"
    return hooks[0].name


def test_crawl_pause_resume_api_cascades_archiveresults_and_leaves_finished_snapshot_results_alone(
    tmp_path,
    client,
    recursive_test_site,
):
    os.chdir(tmp_path)
    init_archive(tmp_path)
    api_token = create_admin_and_token(tmp_path)

    with use_archivebox_db(tmp_path):
        crawl_response = _post_json(
            client,
            "/api/v1/crawls/crawls",
            api_token,
            {
                "urls": [recursive_test_site["root_url"]],
                "max_depth": 0,
                "tags": ["crawl-archiveresult-pause"],
                "config": {"PLUGINS": "wget", "URL_ALLOWLIST": r"127\.0\.0\.1[:/].*"},
            },
        )
        assert crawl_response.status_code == 200, crawl_response.content.decode()
        crawl_id = _json_response(crawl_response)["id"]
        from archivebox.services.runner import run_due_snapshot

        active_response = _post_json(
            client,
            "/api/v1/core/snapshots",
            api_token,
            {
                "url": recursive_test_site["root_url"],
                "crawl_id": crawl_id,
                "depth": 0,
                "title": "Active child",
                "status": "queued",
            },
        )
        assert active_response.status_code == 200, active_response.content.decode()
        active_snapshot = Snapshot.objects.get(id=_json_response(active_response)["id"])

        sealed_response = _post_json(
            client,
            "/api/v1/core/snapshots",
            api_token,
            {
                "url": recursive_test_site["child_urls"][0],
                "crawl_id": crawl_id,
                "depth": 0,
                "title": "Already sealed child",
                "status": "queued",
            },
        )
        assert sealed_response.status_code == 200, sealed_response.content.decode()
        sealed_snapshot_id = _json_response(sealed_response)["id"]
        sealed_snapshot = Snapshot.objects.get(id=sealed_snapshot_id)
        sealed_done = _seed_archiveresult(
            sealed_snapshot,
            plugin="sealedone",
            hook_name="on_Snapshot__sealed_done",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
            output_text="sealed snapshot result remains finished",
            output_path="sealedone/final.txt",
        )
        sealed_snapshot.sm.seal()
        sealed_snapshot.refresh_from_db()
        assert sealed_snapshot.status == Snapshot.StatusChoices.SEALED
        assert sealed_snapshot.retry_at is None

        active_queued = _seed_archiveresult(
            active_snapshot,
            plugin="manualqueue",
            hook_name="on_Snapshot__manual_queue",
            status=ArchiveResult.StatusChoices.QUEUED,
        )
        active_started = _seed_archiveresult(
            active_snapshot,
            plugin="manualstart",
            hook_name="on_Snapshot__manual_start",
            status=ArchiveResult.StatusChoices.STARTED,
        )
        active_done = _seed_archiveresult(
            active_snapshot,
            plugin="manualdone",
            hook_name="on_Snapshot__manual_done",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
            output_text="parent cascade should not rewrite finished rows",
            output_path="manualdone/cascade.txt",
        )
        pause_response = _patch_json(
            client,
            f"/api/v1/crawls/crawl/{crawl_id}",
            api_token,
            {"action": "pause"},
        )
        assert pause_response.status_code == 200, pause_response.content.decode()
        assert _json_response(pause_response)["status"] == Crawl.StatusChoices.PAUSED

        active_snapshot.refresh_from_db()
        sealed_snapshot.refresh_from_db()
        crawl = Crawl.objects.get(id=crawl_id)
        assert crawl.status == Crawl.StatusChoices.PAUSED
        assert crawl.retry_at == RETRY_AT_MAX
        assert active_snapshot.status == Snapshot.StatusChoices.QUEUED
        assert active_snapshot.retry_at is not None
        assert active_snapshot.retry_at <= timezone.now()
        assert ArchiveResult.objects.get(id=active_queued.id).status == ArchiveResult.StatusChoices.QUEUED
        assert ArchiveResult.objects.get(id=active_started.id).status == ArchiveResult.StatusChoices.STARTED

        assert run_due_snapshot(active_snapshot, lock_seconds=60) is True
        active_snapshot.refresh_from_db()
        sealed_snapshot.refresh_from_db()
        assert active_snapshot.status == Snapshot.StatusChoices.PAUSED
        assert active_snapshot.retry_at == RETRY_AT_MAX
        assert sealed_snapshot.status == Snapshot.StatusChoices.SEALED
        assert sealed_snapshot.retry_at is None

        paused_rows = {
            row.plugin: (row.status, row.retry_at) for row in ArchiveResult.objects.filter(id__in=[active_queued.id, active_started.id])
        }
        assert paused_rows == {
            "manualqueue": (ArchiveResult.StatusChoices.PAUSED, RETRY_AT_MAX),
            "manualstart": (ArchiveResult.StatusChoices.PAUSED, RETRY_AT_MAX),
        }

        active_done_row = ArchiveResult.objects.get(id=active_done.id)
        sealed_done_row = ArchiveResult.objects.get(id=sealed_done.id)
        active_done_path = Path(active_snapshot.output_dir) / next(iter(active_done_row.output_files))
        sealed_done_path = Path(sealed_snapshot.output_dir) / next(iter(sealed_done_row.output_files))
        assert active_done_row.status == ArchiveResult.StatusChoices.SUCCEEDED
        assert active_done_row.retry_at is None
        assert active_done_path.read_text() == "parent cascade should not rewrite finished rows"
        assert sealed_done_row.status == ArchiveResult.StatusChoices.SUCCEEDED
        assert sealed_done_row.retry_at is None
        assert sealed_done_path.read_text() == "sealed snapshot result remains finished"

        resume_response = _patch_json(
            client,
            f"/api/v1/crawls/crawl/{crawl_id}",
            api_token,
            {"action": "resume"},
        )
        assert resume_response.status_code == 200, resume_response.content.decode()
        assert _json_response(resume_response)["status"] == Crawl.StatusChoices.QUEUED

        active_snapshot.refresh_from_db()
        sealed_snapshot.refresh_from_db()
        crawl.refresh_from_db()
        assert crawl.status == Crawl.StatusChoices.QUEUED
        assert crawl.retry_at is not None
        assert crawl.retry_at != RETRY_AT_MAX
        assert active_snapshot.status == Snapshot.StatusChoices.QUEUED
        assert active_snapshot.retry_at is not None
        assert active_snapshot.retry_at != RETRY_AT_MAX
        assert sealed_snapshot.status == Snapshot.StatusChoices.SEALED
        assert sealed_snapshot.retry_at is None

        resumed_rows = {
            row.plugin: (row.status, row.retry_at) for row in ArchiveResult.objects.filter(id__in=[active_queued.id, active_started.id])
        }
        assert resumed_rows["manualqueue"][0] == ArchiveResult.StatusChoices.QUEUED
        assert resumed_rows["manualqueue"][1] is not None
        assert resumed_rows["manualqueue"][1] != RETRY_AT_MAX
        assert resumed_rows["manualstart"][0] == ArchiveResult.StatusChoices.QUEUED
        assert resumed_rows["manualstart"][1] is not None
        assert resumed_rows["manualstart"][1] != RETRY_AT_MAX
        assert ArchiveResult.objects.get(id=active_done.id).status == ArchiveResult.StatusChoices.SUCCEEDED
        assert ArchiveResult.objects.get(id=sealed_done.id).status == ArchiveResult.StatusChoices.SUCCEEDED
        assert active_done_path.read_text() == "parent cascade should not rewrite finished rows"
        assert sealed_done_path.read_text() == "sealed snapshot result remains finished"


def test_targeted_extract_retries_one_failed_archiveresult_while_snapshot_stays_paused(
    tmp_path,
    client,
    recursive_test_site,
):
    os.chdir(tmp_path)
    init_archive(tmp_path)
    api_token = create_admin_and_token(tmp_path)

    with use_archivebox_db(tmp_path):
        snapshot_response = _post_json(
            client,
            "/api/v1/core/snapshots",
            api_token,
            {
                "url": recursive_test_site["root_url"],
                "depth": 0,
                "title": "Paused targeted retry",
                "tags": ["targeted-extract-pause"],
                "status": "queued",
            },
        )
        assert snapshot_response.status_code == 200, snapshot_response.content.decode()
        snapshot_id = _json_response(snapshot_response)["id"]
        snapshot = Snapshot.objects.get(id=snapshot_id)

        wget_result = _seed_archiveresult(
            snapshot,
            plugin="wget",
            hook_name=_snapshot_hook_name("wget"),
            status=ArchiveResult.StatusChoices.FAILED,
            output_text="initial failure before targeted retry",
        )
        unrelated_result = _seed_archiveresult(
            snapshot,
            plugin="manualqueue",
            hook_name="on_Snapshot__manual_queue",
            status=ArchiveResult.StatusChoices.QUEUED,
        )
        finished_result = _seed_archiveresult(
            snapshot,
            plugin="manualdone",
            hook_name="on_Snapshot__manual_done",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
            output_text="finished row must survive targeted retry",
            output_path="manualdone/targeted.txt",
        )

        pause_response = _patch_json(
            client,
            f"/api/v1/core/snapshot/{snapshot_id}",
            api_token,
            {"action": "pause"},
        )
        assert pause_response.status_code == 200, pause_response.content.decode()
        assert _json_response(pause_response)["status"] == Snapshot.StatusChoices.PAUSED

        snapshot = Snapshot.objects.get(id=snapshot_id)
        assert snapshot.status == Snapshot.StatusChoices.PAUSED
        assert snapshot.retry_at == RETRY_AT_MAX
        assert ArchiveResult.objects.get(id=wget_result.id).status == ArchiveResult.StatusChoices.FAILED
        assert ArchiveResult.objects.get(id=unrelated_result.id).status == ArchiveResult.StatusChoices.PAUSED
        finished_row = ArchiveResult.objects.get(id=finished_result.id)
        finished_output_path = Path(snapshot.output_dir) / next(iter(finished_row.output_files))
        assert finished_output_path.read_text() == "finished row must survive targeted retry"

    env = build_test_env(
        get_free_port(),
        PLUGINS="wget",
        SAVE_WGET="True",
        WGET_WARC_ENABLED="False",
        URL_ALLOWLIST=r"127\.0\.0\.1[:/].*",
    )
    extract = subprocess.run(
        [sys.executable, "-m", "archivebox", "extract", str(wget_result.id)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=150,
    )
    assert extract.returncode == 0, f"STDOUT:\n{extract.stdout}\nSTDERR:\n{extract.stderr}"

    with use_archivebox_db(tmp_path):
        snapshot = Snapshot.objects.get(id=snapshot_id)
        assert snapshot.status == Snapshot.StatusChoices.PAUSED
        assert snapshot.retry_at == RETRY_AT_MAX

        retried_wget = ArchiveResult.objects.get(id=wget_result.id)
        assert retried_wget.status == ArchiveResult.StatusChoices.SUCCEEDED
        assert retried_wget.output_size > 0
        assert retried_wget.output_files

        unrelated = ArchiveResult.objects.get(id=unrelated_result.id)
        assert unrelated.status == ArchiveResult.StatusChoices.PAUSED
        assert unrelated.retry_at == RETRY_AT_MAX

        finished = ArchiveResult.objects.get(id=finished_result.id)
        assert finished.status == ArchiveResult.StatusChoices.SUCCEEDED
        assert finished.retry_at is None
        assert finished_output_path.read_text() == "finished row must survive targeted retry"
