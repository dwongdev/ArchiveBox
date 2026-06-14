"""Live progress UI tests."""

import subprocess
import uuid
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest
from django.db import connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from archivebox.tests.conftest import ADMIN_TEST_HOST

pytestmark = pytest.mark.django_db


class TestLiveProgressView:
    def test_live_progress_rejects_unauthenticated_unscoped_request(self, client):
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 403
        assert response.json() == {"error": "Permission denied"}
        assert b"orchestrator_running" not in response.content
        assert b"active_crawls" not in response.content
        assert b"traceback" not in response.content

    def test_admin_live_progress_path_does_not_bypass_admin_auth(self, client):
        response = client.get("/admin/live-progress/", HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code in (302, 403, 404)
        assert b"orchestrator_running" not in response.content
        assert b"active_crawls" not in response.content
        assert b"traceback" not in response.content

    @override_settings(DEBUG=False)
    def test_live_progress_error_response_hides_traceback_without_debug(self, client, admin_user, crawl):
        from archivebox.crawls.models import Crawl

        Crawl.objects.filter(pk=crawl.pk).update(
            status=Crawl.StatusChoices.STARTED,
            retry_at=timezone.now(),
            modified_at=timezone.now(),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {Crawl._meta.db_table} SET created_at = %s WHERE id = %s",
                ["not-a-date", str(crawl.pk)],
            )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 500
        payload = response.json()
        assert "error" in payload
        assert "traceback" not in payload
        assert payload["active_crawls"] == []

    def test_live_progress_excludes_old_archiveresults_from_previous_snapshot_run(self, client, admin_user, crawl, snapshot):
        from datetime import timedelta
        from archivebox.core.models import ArchiveResult
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot

        client.force_login(admin_user)

        now = timezone.now()
        Crawl.objects.filter(pk=crawl.pk).update(
            status=Crawl.StatusChoices.STARTED,
            retry_at=now,
            modified_at=now,
        )
        Snapshot.objects.filter(pk=snapshot.pk).update(
            status=Snapshot.StatusChoices.STARTED,
            retry_at=None,
            downloaded_at=now - timedelta(minutes=1),
            modified_at=now,
        )

        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="wget",
            hook_name="on_Snapshot__06_wget.finite.bg",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
            start_ts=now - timedelta(hours=1, minutes=1),
            end_ts=now - timedelta(hours=1),
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="chrome",
            hook_name="on_Snapshot__11_chrome_wait",
            status=ArchiveResult.StatusChoices.QUEUED,
        )

        response = client.get("/progress.json", HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200, response.content
        payload = response.json()
        active_crawl = next(item for item in payload["active_crawls"] if item["id"] == str(crawl.pk))
        active_snapshot = next(item for item in active_crawl["active_snapshots"] if item["id"] == str(snapshot.pk))
        plugin_names = [item["plugin"] for item in active_snapshot["all_plugins"]]
        assert plugin_names == ["chrome"]

    def test_live_progress_does_not_hide_active_snapshot_results_when_modified_at_moves(self, client, admin_user, crawl, snapshot):
        from datetime import timedelta
        from archivebox.core.models import ArchiveResult
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot

        client.force_login(admin_user)

        now = timezone.now()
        Crawl.objects.filter(pk=crawl.pk).update(
            status=Crawl.StatusChoices.STARTED,
            retry_at=now,
            modified_at=now,
        )
        Snapshot.objects.filter(pk=snapshot.pk).update(
            status=Snapshot.StatusChoices.STARTED,
            retry_at=None,
            created_at=now - timedelta(hours=2),
            modified_at=now,
            downloaded_at=None,
        )

        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="wget",
            hook_name="on_Snapshot__06_wget.finite.bg",
            status=ArchiveResult.StatusChoices.STARTED,
            start_ts=now - timedelta(minutes=5),
        )

        response = client.get("/progress.json", HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200, response.content
        payload = response.json()
        active_crawl = next(item for item in payload["active_crawls"] if item["id"] == str(crawl.pk))
        active_snapshot = next(item for item in active_crawl["active_snapshots"] if item["id"] == str(snapshot.pk))
        plugin_names = [item["plugin"] for item in active_snapshot["all_plugins"]]
        assert plugin_names == ["wget"]

    def test_live_progress_hides_finished_cancelled_crawl(self, client, admin_user, crawl, snapshot):
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.crawls.models import Crawl

        now = timezone.now()
        Crawl.objects.filter(pk=crawl.pk).update(
            status=Crawl.StatusChoices.SEALED,
            retry_at=None,
            modified_at=now,
        )
        Snapshot.objects.filter(pk=snapshot.pk).update(
            status=Snapshot.StatusChoices.SEALED,
            retry_at=None,
            downloaded_at=None,
            modified_at=now,
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="singlefile",
            hook_name="on_Snapshot__50_singlefile",
            status=ArchiveResult.StatusChoices.QUEUED,
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        payload = response.json()
        assert payload["active_crawls"] == []
        assert payload["downloads_queued"] == 0
        assert payload["crawls_active"] == 0
        assert payload["archiveresults_queued"] == 1
        assert "crawls_started" not in payload
        assert "crawls_pending" not in payload
        assert "downloads_started" not in payload
        assert "downloads_pending" not in payload

    def test_live_progress_scope_accepts_compact_and_dashed_snapshot_ids(self, client, admin_user, snapshot):
        from archivebox.core.models import Snapshot

        Snapshot.objects.filter(pk=snapshot.pk).update(status=Snapshot.StatusChoices.STARTED)
        compact_id = str(snapshot.id).replace("-", "")
        dashed_id = str(uuid.UUID(hex=compact_id))

        client.force_login(admin_user)
        for snapshot_id in (compact_id, dashed_id):
            response = client.get(reverse("live_progress"), {"snapshot_id": snapshot_id}, HTTP_HOST=ADMIN_TEST_HOST)
            assert response.status_code == 200
            payload = response.json()
            assert payload["scope"]["snapshot_id"] == compact_id
            assert payload["active_crawls"]

    def test_live_progress_scope_accepts_compact_and_dashed_crawl_ids(self, client, admin_user, crawl):
        compact_id = str(crawl.id).replace("-", "")
        dashed_id = str(uuid.UUID(hex=compact_id))

        client.force_login(admin_user)
        for crawl_id in (compact_id, dashed_id):
            response = client.get(reverse("live_progress"), {"crawl_id": crawl_id}, HTTP_HOST=ADMIN_TEST_HOST)
            assert response.status_code == 200
            payload = response.json()
            assert payload["scope"]["crawl_id"] == compact_id
            assert payload["active_crawls"]

    def test_live_progress_shows_old_paused_crawl_with_due_snapshot_work(self, client, admin_user, crawl, snapshot):
        from datetime import timedelta
        from archivebox.crawls.models import Crawl
        from archivebox.core.models import Snapshot

        old_timestamp = timezone.now() - timedelta(days=2)
        Crawl.objects.filter(pk=crawl.pk).update(
            status=Crawl.StatusChoices.PAUSED,
            created_at=old_timestamp,
            modified_at=old_timestamp,
            retry_at=None,
        )
        Snapshot.objects.filter(pk=snapshot.pk).update(
            status=Snapshot.StatusChoices.QUEUED,
            retry_at=timezone.now(),
            modified_at=timezone.now(),
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200, response.content
        payload = response.json()
        active_crawl = next(item for item in payload["active_crawls"] if item["id"] == str(crawl.pk))
        assert active_crawl["status"] == Crawl.StatusChoices.PAUSED
        assert active_crawl["pending_snapshots"] == 1
        assert active_crawl["active_snapshots"] == [
            [
                str(snapshot.pk),
                "https://example.com",
            ],
        ]

    def test_live_progress_reports_real_orchestrator_process_running(self, client, admin_user, db):
        import archivebox.machine.models as machine_models
        from archivebox.machine.models import Machine, Process, psutil

        machine_models._CURRENT_MACHINE = None
        cmd = ["/bin/sleep", "60"]
        popen = subprocess.Popen(
            cmd,
            cwd=Path.cwd(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            os_process = psutil.Process(popen.pid)
            Process.objects.create(
                machine=Machine.current(refresh=True),
                process_type=Process.TypeChoices.ORCHESTRATOR,
                status=Process.StatusChoices.RUNNING,
                pid=popen.pid,
                cmd=cmd,
                env={},
                started_at=datetime.fromtimestamp(os_process.create_time(), tz=dt_timezone.utc),
            )

            client.force_login(admin_user)
            response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

            assert response.status_code == 200
            payload = response.json()
            assert payload["orchestrator_running"] is True
            assert payload["orchestrator_pid"] == popen.pid
        finally:
            if popen.poll() is None:
                popen.terminate()
                try:
                    popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    popen.kill()
                    popen.wait(timeout=5)

    def test_live_progress_ignores_unscoped_running_processes_when_no_crawls(self, client, admin_user, db):
        import os
        import archivebox.machine.models as machine_models
        from archivebox.machine.models import Machine, Process

        machine_models._CURRENT_MACHINE = None
        machine = Machine.current()
        Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=os.getpid(),
            cmd=["/plugins/title/on_Snapshot__10_title.py", "--url=https://example.com"],
            env={},
            started_at=timezone.now(),
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        payload = response.json()
        assert payload["active_crawls"] == []
        assert payload["total_workers"] == 0

    def test_live_progress_does_not_clean_stale_running_processes(self, client, admin_user, db):
        from datetime import timedelta
        import archivebox.machine.models as machine_models
        from archivebox.machine.models import Machine, Process

        machine_models._CURRENT_MACHINE = None
        machine = Machine.current()
        proc = Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=999999,
            cmd=["/plugins/title/on_Snapshot__10_title.py", "--url=https://example.com"],
            env={},
            started_at=timezone.now() - timedelta(days=2),
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        proc.refresh_from_db()
        assert proc.status == Process.StatusChoices.RUNNING
        assert proc.ended_at is None
        assert response.json()["total_workers"] == 0

    def test_live_progress_routes_crawl_process_rows_to_crawl_setup(self, client, admin_user, snapshot, db):
        import os
        import archivebox.machine.models as machine_models
        from archivebox.machine.models import Machine, Process

        machine_models._CURRENT_MACHINE = None
        machine = Machine.current()
        pid = os.getpid()
        Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=pid,
            pwd=str(snapshot.output_dir / "chrome"),
            cmd=["/plugins/chrome/on_CrawlSetup__91_chrome_wait.js", "--url=https://example.com"],
            started_at=timezone.now(),
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        payload = response.json()
        active_crawl = next(crawl for crawl in payload["active_crawls"] if crawl["id"] == str(snapshot.crawl_id))
        setup_entry = next(item for item in active_crawl["setup_plugins"] if item["source"] == "process")
        active_snapshot = next(item for item in active_crawl["active_snapshots"] if item["id"] == str(snapshot.id))
        assert setup_entry["label"] == "chrome wait"
        assert setup_entry["status"] == "started"
        assert active_crawl["worker_pid"] == pid
        assert active_snapshot["all_plugins"] == []

    def test_live_progress_uses_snapshot_process_rows_before_archiveresults(self, client, admin_user, snapshot, db):
        import os
        import archivebox.machine.models as machine_models
        from archivebox.machine.models import Machine, Process

        machine_models._CURRENT_MACHINE = None
        machine = Machine.current()
        pid = os.getpid()
        Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=pid,
            pwd=str(snapshot.output_dir / "title"),
            cmd=["/plugins/title/on_Snapshot__10_title.py", "--url=https://example.com"],
            started_at=timezone.now(),
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        payload = response.json()
        active_crawl = next(crawl for crawl in payload["active_crawls"] if crawl["id"] == str(snapshot.crawl_id))
        active_snapshot = next(item for item in active_crawl["active_snapshots"] if item["id"] == str(snapshot.id))
        assert active_snapshot["all_plugins"][0]["source"] == "process"
        assert active_snapshot["all_plugins"][0]["label"] == "title"
        assert active_snapshot["all_plugins"][0]["status"] == "started"
        assert active_snapshot["worker_pid"] == pid

    def test_live_progress_merges_process_rows_with_archiveresults_when_present(self, client, admin_user, snapshot, db):
        import os
        import archivebox.machine.models as machine_models
        from archivebox.core.models import ArchiveResult
        from archivebox.machine.models import Machine, Process

        machine_models._CURRENT_MACHINE = None
        machine = Machine.current()
        Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.RUNNING,
            pid=os.getpid(),
            pwd=str(snapshot.output_dir / "chrome"),
            cmd=["/plugins/chrome/on_Snapshot__11_chrome_wait.js", "--url=https://example.com"],
            started_at=timezone.now(),
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            status=ArchiveResult.StatusChoices.STARTED,
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        payload = response.json()
        active_crawl = next(crawl for crawl in payload["active_crawls"] if crawl["id"] == str(snapshot.crawl_id))
        active_snapshot = next(item for item in active_crawl["active_snapshots"] if item["id"] == str(snapshot.id))
        sources = {item["source"] for item in active_snapshot["all_plugins"]}
        plugins = {item["plugin"] for item in active_snapshot["all_plugins"]}
        assert sources == {"archiveresult", "process"}
        assert "title" in plugins
        assert "chrome" in plugins

    def test_live_progress_omits_pid_for_exited_process_rows(self, client, admin_user, snapshot, db):
        import archivebox.machine.models as machine_models
        from archivebox.machine.models import Machine, Process

        machine_models._CURRENT_MACHINE = None
        machine = Machine.current()
        Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.EXITED,
            exit_code=0,
            pid=99999,
            pwd=str(snapshot.output_dir / "title"),
            cmd=["/plugins/title/on_Snapshot__10_title.py", "--url=https://example.com"],
            started_at=timezone.now(),
            ended_at=timezone.now(),
        )

        client.force_login(admin_user)
        response = client.get(reverse("live_progress"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        payload = response.json()
        active_crawl = next(crawl for crawl in payload["active_crawls"] if crawl["id"] == str(snapshot.crawl_id))
        active_snapshot = next(item for item in active_crawl["active_snapshots"] if item["id"] == str(snapshot.id))
        process_entry = next(item for item in active_snapshot["all_plugins"] if item["source"] == "process")
        assert process_entry["status"] == "succeeded"
        assert "pid" not in process_entry
