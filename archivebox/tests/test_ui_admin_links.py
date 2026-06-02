import pytest
import subprocess
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from django.contrib.admin.sites import AdminSite
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.urls import reverse
import html
from uuid import uuid4


pytestmark = pytest.mark.django_db


def _create_snapshot():
    from archivebox.base_models.models import get_or_create_system_user_pk
    from archivebox.crawls.models import Crawl
    from archivebox.core.models import Snapshot

    crawl = Crawl.objects.create(
        urls="https://example.com",
        created_by_id=get_or_create_system_user_pk(),
    )
    return Snapshot.objects.create(
        url="https://example.com",
        crawl=crawl,
        status=Snapshot.StatusChoices.STARTED,
    )


def _create_machine():
    from archivebox.machine.models import Machine

    return Machine.objects.create(
        guid=f"test-guid-{uuid4()}",
        hostname="test-host",
        hw_in_docker=False,
        hw_in_vm=False,
        hw_manufacturer="Test",
        hw_product="Test Product",
        hw_uuid=f"test-hw-{uuid4()}",
        os_arch="arm64",
        os_family="darwin",
        os_platform="macOS",
        os_release="14.0",
        os_kernel="Darwin",
        stats={},
        config={},
    )


def _create_iface(machine):
    from archivebox.machine.models import NetworkInterface

    return NetworkInterface.objects.create(
        machine=machine,
        mac_address="00:11:22:33:44:66",
        ip_public="203.0.113.11",
        ip_local="10.0.0.11",
        dns_server="1.1.1.1",
        hostname="test-host",
        iface="en0",
        isp="Test ISP",
        city="Test City",
        region="Test Region",
        country="Test Country",
    )


def _admin_post_request(path):
    request = RequestFactory().post(path)
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


def _admin_get_request(path="/"):
    from archivebox.config.common import get_config

    request = RequestFactory().get(path, HTTP_HOST="admin.archivebox.localhost:8000")
    request.archivebox_config = get_config()
    return request


@pytest.fixture
def running_process_record():
    from archivebox.machine.models import Machine, Process, psutil

    cmd = ["/bin/sleep", "60"]
    popen = subprocess.Popen(
        cmd,
        cwd=Path.cwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        os_process = psutil.Process(popen.pid)
        process = Process.objects.create(
            machine=Machine.current(refresh=True),
            process_type=Process.TypeChoices.HOOK,
            pwd=str(Path.cwd()),
            cmd=cmd,
            pid=popen.pid,
            started_at=datetime.fromtimestamp(os_process.create_time(), tz=dt_timezone.utc),
            status=Process.StatusChoices.RUNNING,
        )
        yield process
    finally:
        if popen.poll() is None:
            popen.terminate()
            try:
                popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                popen.kill()
                popen.wait(timeout=5)


def test_archiveresult_admin_links_plugin_and_process():
    from archivebox.core.admin_archiveresults import ArchiveResultAdmin
    from archivebox.core.models import ArchiveResult
    from archivebox.machine.models import Process

    snapshot = _create_snapshot()
    iface = _create_iface(_create_machine())
    process = Process.objects.create(
        machine=iface.machine,
        iface=iface,
        process_type=Process.TypeChoices.HOOK,
        pwd=str(snapshot.output_dir / "wget"),
        cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
        status=Process.StatusChoices.EXITED,
    )
    result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg.py",
        process=process,
        status=ArchiveResult.StatusChoices.SUCCEEDED,
    )

    admin = ArchiveResultAdmin(ArchiveResult, AdminSite())

    plugin_html = str(admin.plugin_with_icon(result))
    process_html = str(admin.process_link(result))

    assert "/admin/environment/plugins/builtin.wget/" in plugin_html
    assert f"/admin/machine/process/{process.id}/change" in process_html


def test_deleting_binary_and_process_records_preserves_results():
    from archivebox.core.admin_archiveresults import ArchiveResultAdmin, build_abx_dl_replay_command, render_archiveresults_list
    from archivebox.core.models import ArchiveResult
    from archivebox.machine.admin import ProcessAdmin
    from archivebox.machine.models import Binary, Process

    snapshot = _create_snapshot()
    machine = _create_machine()
    binary = Binary.objects.create(
        machine=machine,
        name="wget",
        abspath="/usr/bin/wget",
        version="1.21.2",
        binprovider="env",
        binproviders="env",
        status=Binary.StatusChoices.INSTALLED,
    )
    process = Process.objects.create(
        machine=machine,
        binary=binary,
        process_type=Process.TypeChoices.HOOK,
        pwd=str(snapshot.output_dir / "wget"),
        cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
        status=Process.StatusChoices.EXITED,
    )
    result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg.py",
        process=process,
        status=ArchiveResult.StatusChoices.SUCCEEDED,
    )

    binary.delete()
    process.refresh_from_db()
    assert process.binary_id is None
    assert process.cmd_version == ""
    assert process.bin_abspath == ""
    assert "binary_id" not in process.to_json()
    assert ProcessAdmin(Process, AdminSite()).binary_link(process) == "-"

    process.delete()
    result.refresh_from_db()
    assert result.process_id is None
    assert ArchiveResult.objects.filter(id=result.id).exists()
    assert result.pwd == str(result.output_dir)
    assert result.cmd == []
    assert result.cmd_version == ""
    assert result.binary is None
    assert result.iface is None
    assert result.machine is None
    assert result.timeout == 120
    result_json = result.to_json()
    assert result_json["pwd"] == str(result.output_dir)
    assert "process_id" not in result_json

    admin = ArchiveResultAdmin(ArchiveResult, AdminSite())
    assert admin.process_link(result) == "-"
    assert admin.machine_link(result) == "-"
    assert "cd " in build_abx_dl_replay_command(result)
    assert "wget" in render_archiveresults_list(ArchiveResult.objects.filter(id=result.id))


def test_snapshot_admin_zip_links():
    from archivebox.core.admin_snapshots import SnapshotAdmin
    from archivebox.core.models import Snapshot

    snapshot = _create_snapshot()
    admin = SnapshotAdmin(Snapshot, AdminSite())
    admin.request = _admin_get_request()

    files_url = admin.get_snapshot_files_url(snapshot)
    zip_url = admin.get_snapshot_zip_url(snapshot)

    assert html.escape(zip_url, quote=True) not in str(admin.files(snapshot))
    assert html.escape(files_url, quote=True) in str(admin.size_with_stats(snapshot))
    assert html.escape(zip_url, quote=True) in str(admin.admin_actions(snapshot))


def test_archiveresult_admin_zip_links():
    from archivebox.core.admin_archiveresults import ArchiveResultAdmin
    from archivebox.core.models import ArchiveResult

    snapshot = _create_snapshot()
    result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg.py",
        status=ArchiveResult.StatusChoices.SUCCEEDED,
        output_str="Saved output",
    )

    admin = ArchiveResultAdmin(ArchiveResult, AdminSite())
    admin.request = _admin_get_request()
    zip_url = admin.get_output_zip_url(result)

    assert html.escape(zip_url, quote=True) in str(admin.zip_link(result))
    assert html.escape(zip_url, quote=True) in str(admin.admin_actions(result))


def test_archiveresult_admin_copy_command_redacts_sensitive_env_keys():
    from archivebox.core.admin_archiveresults import ArchiveResultAdmin
    from archivebox.core.models import ArchiveResult
    from archivebox.machine.models import Process

    snapshot = _create_snapshot()
    iface = _create_iface(_create_machine())
    process = Process.objects.create(
        machine=iface.machine,
        iface=iface,
        process_type=Process.TypeChoices.HOOK,
        pwd=str(snapshot.output_dir / "wget"),
        cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
        env={
            "SAFE_FLAG": "1",
            "API_KEY": "super-secret-key",
            "ACCESS_TOKEN": "super-secret-token",
            "SHARED_SECRET": "super-secret-secret",
        },
        status=Process.StatusChoices.EXITED,
        url="https://example.com",
    )
    result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg.py",
        process=process,
        status=ArchiveResult.StatusChoices.SUCCEEDED,
    )

    admin = ArchiveResultAdmin(ArchiveResult, AdminSite())
    admin.request = _admin_get_request()
    cmd_html = str(admin.cmd_str(result))

    assert "SAFE_FLAG=1" in cmd_html
    assert "https://example.com" in cmd_html
    assert "API_KEY" not in cmd_html
    assert "ACCESS_TOKEN" not in cmd_html
    assert "SHARED_SECRET" not in cmd_html
    assert "super-secret-key" not in cmd_html
    assert "super-secret-token" not in cmd_html
    assert "super-secret-secret" not in cmd_html


def test_process_admin_links_binary_and_iface():
    from archivebox.machine.admin import ProcessAdmin
    from archivebox.machine.models import Binary, Process

    machine = _create_machine()
    iface = _create_iface(machine)
    binary = Binary.objects.create(
        machine=machine,
        name="wget",
        abspath="/usr/local/bin/wget",
        version="1.21.2",
        binprovider="env",
        binproviders="env",
        status=Binary.StatusChoices.INSTALLED,
    )
    process = Process.objects.create(
        machine=machine,
        iface=iface,
        binary=binary,
        process_type=Process.TypeChoices.HOOK,
        pwd="/tmp/wget",
        cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
        status=Process.StatusChoices.EXITED,
    )

    admin = ProcessAdmin(Process, AdminSite())

    binary_html = str(admin.binary_link(process))
    iface_html = str(admin.iface_link(process))

    assert f"/admin/machine/binary/{binary.id}/change" in binary_html
    assert f"/admin/machine/networkinterface/{iface.id}/change" in iface_html


def test_process_admin_kill_actions_only_terminate_running_processes(running_process_record):
    from archivebox.machine.admin import ProcessAdmin
    from archivebox.machine.models import Machine, Process

    running = running_process_record
    exited = Process.objects.create(
        machine=Machine.current(),
        process_type=Process.TypeChoices.HOOK,
        pwd="/tmp/exited",
        cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
        status=Process.StatusChoices.EXITED,
    )

    admin = ProcessAdmin(Process, AdminSite())
    request = _admin_post_request("/admin/machine/process/")

    admin.kill_processes(request, Process.objects.filter(pk__in=[running.pk, exited.pk]).order_by("created_at"))

    running.refresh_from_db()
    assert running.status == Process.StatusChoices.EXITED
    assert running.exit_code is not None
    messages = [message.message for message in get_messages(request)]
    assert any("Killed 1 running process" in msg for msg in messages)
    assert any("Skipped 1 process" in msg for msg in messages)


def test_process_admin_object_kill_action_redirects_and_skips_exited():
    from archivebox.machine.admin import ProcessAdmin
    from archivebox.machine.models import Machine, Process

    process = Process.objects.create(
        machine=Machine.current(refresh=True),
        process_type=Process.TypeChoices.HOOK,
        pwd="/tmp/exited",
        cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
        status=Process.StatusChoices.EXITED,
    )

    admin = ProcessAdmin(Process, AdminSite())
    request = _admin_post_request(f"/admin/machine/process/{process.pk}/change/")

    response = admin.kill_process(request, process)

    assert response.status_code == 302
    assert response.url == reverse("admin:machine_process_change", args=[process.pk])
    process.refresh_from_db()
    assert process.status == Process.StatusChoices.EXITED
    messages = [message.message for message in get_messages(request)]
    assert any("Skipped 1 process" in msg for msg in messages)


def test_process_admin_output_summary_uses_archiveresult_output_files():
    from archivebox.core.models import ArchiveResult
    from archivebox.machine.admin import ProcessAdmin
    from archivebox.machine.models import Process

    snapshot = _create_snapshot()
    machine = _create_machine()
    process = Process.objects.create(
        machine=machine,
        process_type=Process.TypeChoices.HOOK,
        pwd=str(snapshot.output_dir / "wget"),
        cmd=["/tmp/on_Snapshot__06_wget.finite.bg.py", "--url=https://example.com"],
        status=Process.StatusChoices.EXITED,
    )
    ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg.py",
        process=process,
        status=ArchiveResult.StatusChoices.SUCCEEDED,
        output_files={
            "index.html": {"extension": "html", "mimetype": "text/html", "size": 1024},
            "title.txt": {"extension": "txt", "mimetype": "text/plain", "size": "512"},
        },
    )

    admin = ProcessAdmin(Process, AdminSite())

    output_html = str(admin.output_summary(process))

    assert "2 files" in output_html
    assert "1.5 KB" in output_html
