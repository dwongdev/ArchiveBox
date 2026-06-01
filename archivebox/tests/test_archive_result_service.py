from pathlib import Path
import pytest


from abxpkg.binary_service import BinaryRequestEvent
from abx_dl.events import ArchiveResultEvent, ProcessEvent, ProcessStartedEvent
from abx_dl.orchestrator import create_bus
from abx_dl.output_files import OutputFile


pytestmark = pytest.mark.django_db(transaction=True)


def _cleanup_machine_process_rows() -> None:
    from archivebox.machine.models import Process

    Process.objects.all().delete()


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


def test_process_completed_projects_inline_archiveresult():
    from archivebox.core.models import ArchiveResult
    from archivebox.services.archive_result_service import ArchiveResultService
    import asyncio

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "wget"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "index.html").write_text("<html>ok</html>")

    bus = create_bus(name="test_inline_archiveresult")
    service = ArchiveResultService(bus)

    event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg",
        status="succeeded",
        output_str="wget/index.html",
        output_files=[OutputFile(path="index.html", extension="html", mimetype="text/html", size=15)],
        start_ts="2026-03-22T12:00:00+00:00",
        end_ts="2026-03-22T12:00:01+00:00",
    )

    async def emit_event() -> None:
        await service.on_ArchiveResultEvent__save_to_db(event)

    asyncio.run(emit_event())

    result = ArchiveResult.objects.get(snapshot=snapshot, plugin="wget", hook_name="on_Snapshot__06_wget.finite.bg")
    assert result.status == ArchiveResult.StatusChoices.SUCCEEDED
    assert result.output_str == "wget/index.html"
    assert "index.html" in result.output_files
    assert result.output_files["index.html"] == {"extension": "html", "mimetype": "text/html", "size": 15}
    assert result.output_size == 15
    _cleanup_machine_process_rows()


def test_archiveresult_event_retry_updates_existing_hook_row():
    from archivebox.core.models import ArchiveResult
    from archivebox.services.archive_result_service import ArchiveResultService
    import asyncio

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "wget"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "index.html").write_text("<html>ok</html>")

    service = ArchiveResultService(create_bus(name="test_archiveresult_retry_updates_existing_hook_row"))
    first_event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg",
        status="failed",
        output_str="timed out",
        start_ts="2026-03-22T12:00:00+00:00",
        end_ts="2026-03-22T12:00:01+00:00",
    )
    retry_event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg",
        status="succeeded",
        output_str="wget/index.html",
        output_files=[OutputFile(path="index.html", extension="html", mimetype="text/html", size=15)],
        start_ts="2026-03-22T12:01:00+00:00",
        end_ts="2026-03-22T12:01:01+00:00",
    )

    async def emit_events() -> None:
        await service.on_ArchiveResultEvent__save_to_db(first_event)
        first_result_id = await ArchiveResult.objects.values_list("id", flat=True).aget(
            snapshot=snapshot,
            plugin="wget",
            hook_name="on_Snapshot__06_wget.finite.bg",
        )
        await service.on_ArchiveResultEvent__save_to_db(retry_event)
        retry_result = await ArchiveResult.objects.aget(
            snapshot=snapshot,
            plugin="wget",
            hook_name="on_Snapshot__06_wget.finite.bg",
        )
        assert retry_result.id == first_result_id
        assert retry_result.status == ArchiveResult.StatusChoices.SUCCEEDED
        assert retry_result.output_str == "wget/index.html"

    asyncio.run(emit_events())

    assert ArchiveResult.objects.filter(snapshot=snapshot, plugin="wget", hook_name="on_Snapshot__06_wget.finite.bg").count() == 1
    _cleanup_machine_process_rows()


def test_archiveresult_duplicate_hook_rows_are_rejected():
    from django.db import IntegrityError, transaction
    from archivebox.core.models import ArchiveResult

    snapshot = _create_snapshot()
    ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg",
        status=ArchiveResult.StatusChoices.FAILED,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="wget",
            hook_name="on_Snapshot__06_wget.finite.bg",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
        )


def test_process_completed_projects_synthetic_failed_archiveresult():
    from archivebox.core.models import ArchiveResult
    from archivebox.services.archive_result_service import ArchiveResultService
    import asyncio

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "chrome"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    bus = create_bus(name="test_synthetic_archiveresult")
    service = ArchiveResultService(bus)

    event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="chrome",
        hook_name="on_Snapshot__11_chrome_wait",
        status="failed",
        output_str="Hook timed out after 60 seconds",
        error="Hook timed out after 60 seconds",
        start_ts="2026-03-22T12:00:00+00:00",
        end_ts="2026-03-22T12:01:00+00:00",
    )

    async def emit_event() -> None:
        await service.on_ArchiveResultEvent__save_to_db(event)

    asyncio.run(emit_event())

    result = ArchiveResult.objects.get(snapshot=snapshot, plugin="chrome", hook_name="on_Snapshot__11_chrome_wait")
    assert result.status == ArchiveResult.StatusChoices.FAILED
    assert result.output_str == "Hook timed out after 60 seconds"
    assert "Hook timed out" in result.notes
    _cleanup_machine_process_rows()


def test_failed_title_archiveresult_does_not_overwrite_snapshot_title():
    from archivebox.core.models import ArchiveResult
    from archivebox.services.archive_result_service import ArchiveResultService
    import asyncio

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "title"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    bus = create_bus(name="test_failed_title_does_not_update_snapshot")
    service = ArchiveResultService(bus)

    event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="title",
        hook_name="on_Snapshot__54_title.js",
        status="failed",
        output_str="No Chrome session found (chrome plugin must run first)",
        error="No Chrome session found (chrome plugin must run first)",
        start_ts="2026-03-22T12:00:00+00:00",
        end_ts="2026-03-22T12:00:01+00:00",
    )

    async def emit_event() -> None:
        await service.on_ArchiveResultEvent__save_to_db(event)

    asyncio.run(emit_event())

    result = ArchiveResult.objects.get(snapshot=snapshot, plugin="title", hook_name="on_Snapshot__54_title.js")
    assert result.status == ArchiveResult.StatusChoices.FAILED
    assert result.output_str == "No Chrome session found (chrome plugin must run first)"
    snapshot.refresh_from_db()
    assert snapshot.title in (None, "")
    assert snapshot.resolved_title == ""
    _cleanup_machine_process_rows()


def test_snapshot_resolved_title_ignores_failed_title_output_str():
    from archivebox.core.models import ArchiveResult

    snapshot = _create_snapshot()
    ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="title",
        hook_name="on_Snapshot__54_title.js",
        status=ArchiveResult.StatusChoices.FAILED,
        output_str="No Chrome session found (chrome plugin must run first)",
    )

    snapshot.refresh_from_db()
    assert snapshot.title in (None, "")
    assert snapshot.resolved_title == ""
    _cleanup_machine_process_rows()


def test_snapshot_save_normalizes_url_title_to_none():
    from archivebox.core.models import Snapshot

    snapshot = _create_snapshot()
    snapshot.title = snapshot.url
    snapshot.save(update_fields=["title", "modified_at"])

    snapshot.refresh_from_db()
    assert snapshot.title is None
    assert snapshot.resolved_title == ""

    created = Snapshot.objects.create(
        url="https://example.com/title-normalize-create",
        title="https://example.com/title-normalize-create",
        crawl=snapshot.crawl,
    )

    created.refresh_from_db()
    assert created.title is None
    assert created.resolved_title == ""
    _cleanup_machine_process_rows()


def test_process_completed_projects_noresults_archiveresult():
    from archivebox.core.models import ArchiveResult
    from archivebox.services.archive_result_service import ArchiveResultService
    import asyncio

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "title"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    bus = create_bus(name="test_noresults_archiveresult")
    service = ArchiveResultService(bus)

    event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="title",
        hook_name="on_Snapshot__54_title.js",
        status="noresults",
        output_str="No title found",
        start_ts="2026-03-22T12:00:00+00:00",
        end_ts="2026-03-22T12:00:01+00:00",
    )

    async def emit_event() -> None:
        await service.on_ArchiveResultEvent__save_to_db(event)

    asyncio.run(emit_event())

    result = ArchiveResult.objects.get(snapshot=snapshot, plugin="title", hook_name="on_Snapshot__54_title.js")
    assert result.status == ArchiveResult.StatusChoices.NORESULTS
    assert result.output_str == "No title found"


def test_retry_failed_archiveresults_requeues_snapshot_in_queued_state():
    from archivebox.core.models import ArchiveResult, Snapshot

    snapshot = _create_snapshot()
    ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="chrome",
        hook_name="on_Snapshot__11_chrome_wait",
        status=ArchiveResult.StatusChoices.FAILED,
        output_str="timed out",
        output_files={"stderr.log": {}},
        output_size=123,
        output_mimetypes="text/plain",
    )

    reset_count = snapshot.retry_failed_archiveresults()

    snapshot.refresh_from_db()
    result = ArchiveResult.objects.get(snapshot=snapshot, plugin="chrome", hook_name="on_Snapshot__11_chrome_wait")
    assert reset_count == 1
    assert snapshot.status == Snapshot.StatusChoices.QUEUED
    assert snapshot.retry_at is not None
    assert snapshot.current_step == 0
    assert result.status == ArchiveResult.StatusChoices.QUEUED
    assert result.output_str == ""
    assert result.output_json is None
    assert result.output_files == {}
    assert result.output_size == 0
    assert result.output_mimetypes == ""
    assert result.start_ts is None
    assert result.end_ts is None
    snapshot.refresh_from_db()
    assert snapshot.title in (None, "")
    _cleanup_machine_process_rows()


def test_retry_failed_archiveresults_preserves_legacy_plugin_rows_without_hook_name():
    from archivebox.core.models import ArchiveResult, Snapshot

    snapshot = _create_snapshot()
    legacy_result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="",
        status=ArchiveResult.StatusChoices.FAILED,
        output_str="legacy failure",
        output_files={"index.html": {"size": 123}},
        output_size=123,
        output_mimetypes="text/html",
    )
    hook_result = ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="wget",
        hook_name="on_Snapshot__06_wget.finite.bg",
        status=ArchiveResult.StatusChoices.FAILED,
        output_str="hook failure",
        output_files={"stderr.log": {}},
        output_size=10,
        output_mimetypes="text/plain",
    )

    reset_count = snapshot.retry_failed_archiveresults()

    snapshot.refresh_from_db()
    snapshot.crawl.refresh_from_db()
    legacy_result.refresh_from_db()
    hook_result.refresh_from_db()

    assert reset_count == 2
    assert snapshot.status == Snapshot.StatusChoices.QUEUED
    assert snapshot.retry_at is not None
    assert snapshot.crawl.status == snapshot.crawl.StatusChoices.QUEUED
    assert snapshot.crawl.retry_at is not None
    assert legacy_result.status == ArchiveResult.StatusChoices.FAILED
    assert legacy_result.output_str == "legacy failure"
    assert legacy_result.output_files == {"index.html": {"size": 123}}
    assert legacy_result.output_size == 123
    assert hook_result.status == ArchiveResult.StatusChoices.QUEUED
    assert hook_result.output_str == ""
    assert hook_result.output_files == {}
    assert hook_result.output_size == 0
    _cleanup_machine_process_rows()


def test_process_completed_projects_snapshot_title_from_output_str():
    from archivebox.services.archive_result_service import ArchiveResultService
    import asyncio

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "title"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    bus = create_bus(name="test_snapshot_title_output_str")
    service = ArchiveResultService(bus)

    event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="title",
        hook_name="on_Snapshot__54_title.js",
        status="succeeded",
        output_str="Example Domain",
        start_ts="2026-03-22T12:00:00+00:00",
        end_ts="2026-03-22T12:00:01+00:00",
    )

    async def emit_event() -> None:
        await service.on_ArchiveResultEvent__save_to_db(event)

    asyncio.run(emit_event())

    snapshot.refresh_from_db()
    assert snapshot.title == "Example Domain"
    _cleanup_machine_process_rows()


def test_process_completed_projects_snapshot_title_from_title_file():
    from archivebox.services.archive_result_service import ArchiveResultService
    import asyncio

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "title"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "title.txt").write_text("Example Domain")

    bus = create_bus(name="test_snapshot_title_file")
    service = ArchiveResultService(bus)

    event = ArchiveResultEvent(
        snapshot_id=str(snapshot.id),
        plugin="title",
        hook_name="on_Snapshot__54_title.js",
        status="noresults",
        output_str="No title found",
        output_files=[OutputFile(path="title.txt", extension="txt", mimetype="text/plain", size=14)],
        start_ts="2026-03-22T12:00:00+00:00",
        end_ts="2026-03-22T12:00:01+00:00",
    )

    async def emit_event() -> None:
        await service.on_ArchiveResultEvent__save_to_db(event)

    asyncio.run(emit_event())

    snapshot.refresh_from_db()
    assert snapshot.title == "Example Domain"
    _cleanup_machine_process_rows()


def test_snapshot_resolved_title_falls_back_to_title_file_without_db_title():
    from archivebox.core.models import ArchiveResult

    snapshot = _create_snapshot()
    plugin_dir = Path(snapshot.output_dir) / "title"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "title.txt").write_text("Example Domain")
    ArchiveResult.objects.create(
        snapshot=snapshot,
        plugin="title",
        hook_name="on_Snapshot__54_title.js",
        status="noresults",
        output_str="No title found",
        output_files={"title.txt": {}},
    )

    snapshot.refresh_from_db()
    assert snapshot.title in (None, "")
    assert snapshot.resolved_title == "Example Domain"
    _cleanup_machine_process_rows()


def test_collect_output_metadata_preserves_file_metadata():
    from archivebox.services.archive_result_service import _resolve_output_metadata

    output_files, output_size, output_mimetypes = _resolve_output_metadata(
        [OutputFile(path="index.html", extension="html", mimetype="text/html", size=42)],
        Path("/tmp/does-not-need-to-exist"),
    )

    assert output_files == {
        "index.html": {
            "extension": "html",
            "mimetype": "text/html",
            "size": 42,
        },
    }
    assert output_size == 42
    assert output_mimetypes == "text/html"


def test_collect_output_metadata_detects_warc_gz_mimetype(tmp_path):
    from archivebox.services.archive_result_service import _collect_output_metadata

    plugin_dir = tmp_path / "wget"
    warc_file = plugin_dir / "warc" / "capture.warc.gz"
    warc_file.parent.mkdir(parents=True, exist_ok=True)
    warc_file.write_bytes(b"warc-bytes")

    output_files, output_size, output_mimetypes = _collect_output_metadata(plugin_dir)

    assert output_files["warc/capture.warc.gz"] == {
        "extension": "gz",
        "mimetype": "application/warc",
        "size": 10,
    }
    assert output_size == 10
    assert output_mimetypes == "application/warc"


@pytest.mark.django_db(transaction=True)
def test_process_started_hydrates_binary_and_iface_from_existing_binary_records(tmp_path):
    from archivebox.machine.models import Binary, NetworkInterface
    from archivebox.machine.models import Process as MachineProcess
    from archivebox.services.process_service import ProcessService as ArchiveBoxProcessService
    from abx_dl.services.process_service import ProcessService as DlProcessService

    iface = NetworkInterface.current()
    machine = iface.machine

    binary = Binary.objects.create(
        machine=machine,
        name="postlight-parser",
        abspath="/tmp/postlight-parser",
        version="2.2.3",
        binprovider="npm",
        binproviders="npm",
        status=Binary.StatusChoices.INSTALLED,
    )

    hook_path = tmp_path / "on_Snapshot__57_mercury.py"
    hook_path.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    hook_path.chmod(0o755)
    output_dir = tmp_path / "mercury"
    output_dir.mkdir()

    bus = create_bus(name="test_process_started_binary_hydration")
    DlProcessService(bus, emit_jsonl=False, interactive_tty=False)
    ArchiveBoxProcessService(bus)

    async def run_test() -> None:
        await bus.emit(
            ProcessEvent(
                plugin_name="mercury",
                hook_name="on_Snapshot__57_mercury.py",
                hook_path=str(hook_path),
                hook_args=["--url=https://example.com"],
                is_background=False,
                output_dir=str(output_dir),
                env={
                    "MERCURY_BINARY": binary.abspath,
                    "NODE_BINARY": "/tmp/node",
                },
                timeout=60,
                url="https://example.com",
            ),
        ).now()
        started = await bus.find(
            ProcessStartedEvent,
            past=True,
            future=False,
            hook_name="on_Snapshot__57_mercury.py",
            output_dir=str(output_dir),
        )
        assert started is not None
        await started.wait()
        await started.event_results_list()

    import asyncio

    asyncio.run(run_test())

    process = MachineProcess.objects.get(
        pwd=str(output_dir),
        cmd=[str(hook_path), "--url=https://example.com"],
    )
    assert process.binary_id == binary.id
    assert process.iface_id == iface.id


@pytest.mark.django_db(transaction=True)
def test_process_started_uses_node_binary_for_js_hooks_without_plugin_binary(tmp_path):
    from archivebox.machine.models import Binary, NetworkInterface
    from archivebox.machine.models import Process as MachineProcess
    from archivebox.services.process_service import ProcessService as ArchiveBoxProcessService
    from abx_dl.services.process_service import ProcessService as DlProcessService

    iface = NetworkInterface.current()
    machine = iface.machine

    node = Binary.objects.create(
        machine=machine,
        name="node",
        abspath="/tmp/node",
        version="22.0.0",
        binprovider="env",
        binproviders="env",
        status=Binary.StatusChoices.INSTALLED,
    )

    hook_path = tmp_path / "on_Snapshot__75_parse_dom_outlinks.js"
    hook_path.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    hook_path.chmod(0o755)
    output_dir = tmp_path / "parse-dom-outlinks"
    output_dir.mkdir()

    bus = create_bus(name="test_process_started_node_fallback")
    DlProcessService(bus, emit_jsonl=False, interactive_tty=False)
    ArchiveBoxProcessService(bus)

    async def run_test() -> None:
        await bus.emit(
            ProcessEvent(
                plugin_name="parse_dom_outlinks",
                hook_name="on_Snapshot__75_parse_dom_outlinks.js",
                hook_path=str(hook_path),
                hook_args=["--url=https://example.com"],
                is_background=False,
                output_dir=str(output_dir),
                env={"NODE_BINARY": node.abspath},
                timeout=60,
                url="https://example.com",
            ),
        ).now()
        started = await bus.find(
            ProcessStartedEvent,
            past=True,
            future=False,
            hook_name="on_Snapshot__75_parse_dom_outlinks.js",
            output_dir=str(output_dir),
        )
        assert started is not None
        await started.wait()
        await started.event_results_list()

    import asyncio

    asyncio.run(run_test())

    process = MachineProcess.objects.get(
        pwd=str(output_dir),
        cmd=[str(hook_path), "--url=https://example.com"],
    )
    assert process.binary_id == node.id
    assert process.iface_id == iface.id


def test_binary_event_reuses_existing_installed_binary_row():
    from archivebox.machine.models import Binary, Machine
    from archivebox.services.binary_service import ArchiveBoxDBBinaryCacheBackend
    from abxpkg.binary_service import BinaryCacheService, BinaryService
    import asyncio

    machine = Machine.current()

    binary = Binary.objects.create(
        machine=machine,
        name="wget",
        abspath="/bin/sh",
        version="9.9.9",
        binprovider="env",
        binproviders="env,apt,brew",
        status=Binary.StatusChoices.INSTALLED,
    )

    bus = create_bus(name="test_binary_event_reuses_existing_installed_binary_row")
    BinaryCacheService(bus, backend=ArchiveBoxDBBinaryCacheBackend())
    BinaryService(bus)
    event = BinaryRequestEvent(
        name="wget",
        binproviders=binary.binproviders,
        extra_context={
            "plugin_name": "wget",
            "output_dir": "/tmp/wget",
        },
    )

    async def run_event():
        await bus.emit(event).now()
        await bus.wait_until_idle()

    asyncio.run(run_event())

    binary.refresh_from_db()
    assert Binary.objects.filter(machine=machine, name="wget").count() == 1
    assert binary.status == Binary.StatusChoices.INSTALLED
    assert binary.abspath == "/bin/sh"
    assert binary.version == "9.9.9"
    assert binary.binprovider == "env"
    assert binary.binproviders == "env,apt,brew"
