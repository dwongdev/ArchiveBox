"""Machine, binary, and process admin UI tests."""

import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from archivebox.tests.conftest import ADMIN_TEST_HOST

pytestmark = pytest.mark.django_db


class TestMachineAdmin:
    def test_binary_change_view_renders(self, client, admin_user, db):
        """Binary admin change form should load without FieldError."""
        from archivebox.machine.models import Machine, Binary

        machine = Machine.objects.create(
            guid=f"test-guid-{uuid.uuid4()}",
            hostname="test-host",
            hw_in_docker=False,
            hw_in_vm=False,
            hw_manufacturer="Test",
            hw_product="Test Product",
            hw_uuid=f"test-hw-{uuid.uuid4()}",
            os_arch="x86_64",
            os_family="darwin",
            os_platform="darwin",
            os_release="test",
            os_kernel="test-kernel",
            stats={},
        )
        binary = Binary.objects.create(
            machine=machine,
            name="gallery-dl",
            binproviders="env",
            binprovider="env",
            abspath="/opt/homebrew/bin/gallery-dl",
            version="1.26.9",
            sha256="abc123",
            status=Binary.StatusChoices.INSTALLED,
        )

        client.force_login(admin_user)
        url = f"/admin/machine/binary/{binary.pk}/change/"
        response = client.get(url, HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        assert b"gallery-dl" in response.content

    def test_process_change_view_renders_copyable_cmd_env_and_readonly_runtime_fields(self, client, admin_user, db):
        from datetime import timedelta
        from archivebox.machine.models import Machine, Process

        machine = Machine.objects.create(
            guid=f"test-guid-{uuid.uuid4()}",
            hostname="test-host",
            hw_in_docker=False,
            hw_in_vm=False,
            hw_manufacturer="Test",
            hw_product="Test Product",
            hw_uuid=f"test-hw-{uuid.uuid4()}",
            os_arch="x86_64",
            os_family="darwin",
            os_platform="darwin",
            os_release="test",
            os_kernel="test-kernel",
            stats={},
        )
        process = Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.EXITED,
            pwd="/tmp/archivebox",
            cmd=["python", "/tmp/job.py", "--url=https://example.com"],
            env={
                "ENABLED": True,
                "API_KEY": "super-secret-key",
                "ACCESS_TOKEN": "super-secret-token",
                "SHARED_SECRET": "super-secret-secret",
            },
            timeout=90,
            pid=54321,
            exit_code=0,
            url="https://example.com/status",
            started_at=timezone.now() - timedelta(seconds=52),
            ended_at=timezone.now(),
        )

        client.force_login(admin_user)
        url = reverse("admin:machine_process_change", args=[process.pk])
        response = client.get(url, HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        assert b"Kill" in response.content
        assert b"python /tmp/job.py --url=https://example.com" in response.content
        assert b"ENABLED=True" in response.content
        assert b"52s" in response.content
        assert b"API_KEY=" not in response.content
        assert b"ACCESS_TOKEN=" not in response.content
        assert b"SHARED_SECRET=" not in response.content
        assert b"super-secret-key" not in response.content
        assert b"super-secret-token" not in response.content
        assert b"super-secret-secret" not in response.content
        assert response.content.count(b"data-command=") >= 2
        assert b'name="timeout"' not in response.content
        assert b'name="pid"' not in response.content
        assert b'name="exit_code"' not in response.content
        assert b'name="url"' not in response.content
        assert b'name="started_at"' not in response.content
        assert b'name="ended_at"' not in response.content

    def test_process_kill_object_action_is_post_only(self, admin_client, db):
        from archivebox.machine.models import Machine, Process

        process = Process.objects.create(
            machine=Machine.current(refresh=True),
            process_type=Process.TypeChoices.HOOK,
            pwd="/tmp/exited",
            cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
            status=Process.StatusChoices.EXITED,
        )
        action_url = reverse("admin:machine_process_actions", kwargs={"pk": process.pk, "tool": "kill_process"})

        change_response = admin_client.get(reverse("admin:machine_process_change", args=[process.pk]), HTTP_HOST=ADMIN_TEST_HOST)
        assert change_response.status_code == 200
        assert b'<form method="post"' in change_response.content
        assert action_url.encode() in change_response.content

        get_response = admin_client.get(action_url, HTTP_HOST=ADMIN_TEST_HOST)
        assert get_response.status_code == 405

        post_response = admin_client.post(action_url, HTTP_HOST=ADMIN_TEST_HOST)
        assert post_response.status_code == 302
        assert post_response.url == reverse("admin:machine_process_change", args=[process.pk])
        process.refresh_from_db()
        assert process.status == Process.StatusChoices.EXITED

    def test_process_list_view_shows_duration_snapshot_and_crawl_columns(self, client, admin_user, snapshot, db):
        from datetime import timedelta
        from archivebox.core.models import ArchiveResult
        from archivebox.machine.models import Machine, Process

        machine = Machine.objects.create(
            guid=f"list-guid-{uuid.uuid4()}",
            hostname="list-host",
            hw_in_docker=False,
            hw_in_vm=False,
            hw_manufacturer="Test",
            hw_product="Test Product",
            hw_uuid=f"list-hw-{uuid.uuid4()}",
            os_arch="x86_64",
            os_family="darwin",
            os_platform="darwin",
            os_release="test",
            os_kernel="test-kernel",
            stats={},
        )
        process = Process.objects.create(
            machine=machine,
            process_type=Process.TypeChoices.HOOK,
            status=Process.StatusChoices.EXITED,
            pwd="/tmp/archivebox",
            cmd=["python", "/tmp/job.py"],
            env={},
            pid=12345,
            exit_code=0,
            started_at=timezone.now() - timedelta(milliseconds=10),
            ended_at=timezone.now(),
        )
        ArchiveResult.objects.create(
            snapshot=snapshot,
            process=process,
            plugin="title",
            hook_name="on_Snapshot__54_title",
            status="succeeded",
            output_str="Example Domain",
        )

        client.force_login(admin_user)
        response = client.get(reverse("admin:machine_process_changelist"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        assert b"Duration" in response.content
        assert b"Snapshot" in response.content
        assert b"Crawl" in response.content
        assert b"0.01s" in response.content
        changelist = response.context["cl"]
        row = next(obj for obj in changelist.result_list if obj.pk == process.pk)

        assert row.archiveresult.snapshot_id == snapshot.id
        assert str(snapshot.id) in str(changelist.model_admin.snapshot_link(row))
        assert str(snapshot.crawl_id) in str(changelist.model_admin.crawl_link(row))
