from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asgiref.sync import sync_to_async
from django.utils import timezone

from abxpkg import Binary as AbxBinary
from abxpkg import BinProvider, PROVIDER_CLASS_BY_NAME
from abxpkg.binary_service import BinaryEvent, BinaryRequestEvent
from abxbus import BaseEvent, EventBus
from abx_dl.services.base import BaseService


_LIB_DIR_MANAGED_PROVIDERS = {
    "bash",
    "cargo",
    "deno",
    "gem",
    "goget",
    "chromewebstore",
    "nix",
    "npm",
    "pip",
    "puppeteer",
    "uv",
}


class ArchiveBoxDBBinaryCacheBackend:
    """ArchiveBox machine.Binary projection backend for abxpkg BinaryCacheService."""

    async def get(self, request: BinaryRequestEvent) -> AbxBinary | None:
        from archivebox.config.common import get_config
        from archivebox.machine.models import Binary, Machine, _canonical_binary_name

        machine = await sync_to_async(Machine.current, thread_sensitive=True)()
        binary_name = _canonical_binary_name(request.name)
        if not binary_name:
            return None

        existing = await Binary.objects.filter(machine=machine, name=binary_name).afirst()
        cache_invalidated = False
        if existing and existing.status == Binary.StatusChoices.INSTALLED:
            changed = False
            requested_binproviders = _binproviders_to_str(request.binproviders)
            if requested_binproviders and existing.binproviders != requested_binproviders:
                existing.binproviders = requested_binproviders
                changed = True
            if request.overrides and existing.overrides != request.overrides:
                existing.overrides = request.overrides
                changed = True
            if changed:
                existing.status = Binary.StatusChoices.QUEUED
                existing.retry_at = None
                cache_invalidated = True
                await existing.asave(update_fields=["binproviders", "overrides", "status", "retry_at", "modified_at"])
        elif existing is None:
            await Binary.objects.acreate(
                machine=machine,
                name=binary_name,
                binproviders=_binproviders_to_str(request.binproviders),
                overrides=request.overrides or {},
                status=Binary.StatusChoices.QUEUED,
            )

        installed = None
        if not cache_invalidated:
            installed = (
                await Binary.objects.filter(machine=machine, name=binary_name, status=Binary.StatusChoices.INSTALLED)
                .exclude(abspath="")
                .exclude(abspath__isnull=True)
                .order_by("-modified_at")
                .afirst()
            )
            if installed is not None and not await sync_to_async(Path(installed.abspath).expanduser().exists, thread_sensitive=True)():
                installed.status = Binary.StatusChoices.QUEUED
                installed.retry_at = None
                await installed.asave(update_fields=["status", "retry_at", "modified_at"])
                installed = None
            if installed is not None and request.overrides and installed.overrides != request.overrides:
                installed.status = Binary.StatusChoices.QUEUED
                installed.retry_at = None
                await installed.asave(update_fields=["status", "retry_at", "modified_at"])
                installed = None
        if installed is None:
            return None

        installed_path = Path(installed.abspath).expanduser().resolve(strict=False)
        active_lib_dir = (
            Path(str((await sync_to_async(get_config, thread_sensitive=True)()).get("LIB_DIR", ""))).expanduser().resolve(strict=False)
        )
        provider_name = (installed.binprovider or installed.binproviders.split(",", 1)[0]).strip()
        if active_lib_dir and provider_name in _LIB_DIR_MANAGED_PROVIDERS:
            try:
                installed_path.relative_to(active_lib_dir)
            except ValueError:
                installed.status = Binary.StatusChoices.QUEUED
                installed.retry_at = None
                await installed.asave(update_fields=["status", "retry_at", "modified_at"])
                return None

        provider = _provider_for_name(provider_name, installed.name, installed.overrides)
        binary_env = BinProvider.build_exec_env(providers=[provider], base_env={}) if provider is not None else {}
        provider_names = _provider_names(installed.binproviders or request.binproviders or "env")
        return AbxBinary.model_validate(
            {
                "name": request.name,
                "description": request.description,
                "binproviders": _providers_for_names(provider_names),
                "overrides": installed.overrides or request.overrides or {},
                "loaded_binprovider": provider,
                "loaded_abspath": installed.abspath,
                "loaded_version": installed.version or None,
                "loaded_sha256": installed.sha256 or None,
                "env": binary_env,
            },
        )

    async def set(self, request: BinaryRequestEvent | None, binary: AbxBinary) -> None:
        from archivebox.config.common import get_config
        from archivebox.machine.models import Binary, Machine, _canonical_binary_name

        machine = await sync_to_async(Machine.current, thread_sensitive=True)()
        binary_name = _canonical_binary_name(binary.name)
        if not binary_name:
            return
        request_context = request.extra_context if request is not None else {}
        binary_id = str(request_context.get("binary_id") or "")
        if binary_id:
            existing = await Binary.objects.filter(id=binary_id).afirst()
        else:
            existing = None
        if existing is None:
            existing, _created = await Binary.objects.aget_or_create(
                machine=machine,
                name=binary_name,
                defaults={"status": Binary.StatusChoices.QUEUED},
            )

        existing.abspath = str(binary.loaded_abspath or "")
        if binary.loaded_version:
            existing.version = str(binary.loaded_version)
        if binary.loaded_sha256:
            existing.sha256 = str(binary.loaded_sha256)
        existing.binproviders = _binproviders_to_str(
            request.binproviders if request is not None else [provider.name for provider in binary.binproviders],
        )
        if binary.loaded_binprovider is not None:
            existing.binprovider = binary.loaded_binprovider.name
        existing.overrides = request.overrides if request is not None and request.overrides is not None else binary.overrides
        existing.status = Binary.StatusChoices.INSTALLED
        existing.retry_at = None
        await existing.asave(
            update_fields=["abspath", "version", "sha256", "binproviders", "binprovider", "overrides", "status", "retry_at", "modified_at"],
        )
        lib_bin_dir = await sync_to_async(lambda: get_config().LIB_BIN_DIR, thread_sensitive=True)()
        await sync_to_async(existing.symlink_to_lib_bin_after_commit, thread_sensitive=True)(lib_bin_dir)

    async def invalidate(self, request: BinaryRequestEvent, binary: AbxBinary, reason: str) -> None:
        from archivebox.machine.models import Binary, Machine, _canonical_binary_name

        machine = await sync_to_async(Machine.current, thread_sensitive=True)()
        binary_name = _canonical_binary_name(request.name)
        if not binary_name:
            return
        installed = (
            await Binary.objects.filter(machine=machine, name=binary_name, status=Binary.StatusChoices.INSTALLED)
            .exclude(abspath="")
            .exclude(abspath__isnull=True)
            .order_by("-modified_at")
            .afirst()
        )
        if installed is None:
            return
        installed.status = Binary.StatusChoices.QUEUED
        installed.retry_at = None
        await installed.asave(update_fields=["status", "retry_at", "modified_at"])


class ArchiveBoxBinaryService(BaseService):
    """Preserve ArchiveBox's legacy Binary Process rows around abxpkg requests."""

    LISTENS_TO = [BinaryRequestEvent]
    EMITS: list[type[BaseEvent]] = []

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self.bus.on(BinaryRequestEvent, self.on_BinaryRequestEvent__project_process)

    async def on_BinaryRequestEvent__project_process(self, request: BinaryRequestEvent) -> None:
        from archivebox.machine.models import Binary, Machine, Process, _canonical_binary_name
        from archivebox.services.process_service import current_network_interface_with_machine

        machine = await sync_to_async(Machine.current, thread_sensitive=True)()
        binary_name = _canonical_binary_name(request.name)
        if not binary_name:
            return
        binary = await Binary.objects.filter(machine=machine, name=binary_name).order_by("-modified_at").afirst()
        if binary is None:
            return

        binary_event = await self.bus.find(
            BinaryEvent,
            child_of=request,
            past=True,
            future=False,
            name=request.name,
            where=lambda candidate: bool(candidate.abspath),
        )
        iface = await sync_to_async(current_network_interface_with_machine, thread_sensitive=True)()
        now = timezone.now()
        success = isinstance(binary_event, BinaryEvent)
        output_dir = self._process_output_dir(binary, request)
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "abxpkg",
            "install",
            f"--name={request.name}",
            f"--binproviders={_binproviders_to_str(request.binproviders)}",
        ]
        if request.overrides:
            cmd.append(f"--overrides={json.dumps(request.overrides, sort_keys=True)}")
        stdout = json.dumps(binary.to_json()) + "\n" if success else ""
        stderr = "" if success else f"Binary request did not resolve: {request.name}"
        process = await Process.objects.acreate(
            machine=iface.machine,
            iface=iface,
            process_type=Process.TypeChoices.BINARY,
            worker_type="",
            pwd=str(output_dir),
            cmd=cmd,
            env={},
            timeout=int(request.event_timeout or request.install_timeout or 600),
            pid=None,
            url=None,
            started_at=now,
            ended_at=now,
            stdout=stdout,
            stderr=stderr,
            exit_code=0 if success else 1,
            status=Process.StatusChoices.EXITED,
            retry_at=None,
            binary=binary,
        )
        self._write_binary_index(binary, process, output_dir)

    def _process_output_dir(self, binary, request: BinaryRequestEvent) -> Path:
        raw_output_dir = str(request.extra_context.get("output_dir") or "").strip()
        if raw_output_dir:
            output_dir = Path(raw_output_dir).expanduser()
            if output_dir.name == str(binary.id):
                return output_dir.parent
            return output_dir
        return binary.output_dir.parent

    def _write_binary_index(self, binary, process, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        index_path = output_dir / "index.jsonl"
        with index_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(binary.to_json()) + "\n")
            f.write(json.dumps(process.to_json()) + "\n")


def _provider_names(binproviders: str | list[str] | None) -> list[str]:
    if isinstance(binproviders, str):
        raw_names = [part.strip() for part in binproviders.split(",")]
    elif binproviders:
        raw_names = [str(part).strip() for part in binproviders]
    else:
        raw_names = ["env"]
    names: list[str] = []
    for name in raw_names:
        if name and name not in names:
            names.append(name)
    return names or ["env"]


def _binproviders_to_str(binproviders: str | list[str] | None) -> str:
    return ",".join(_provider_names(binproviders))


def _providers_for_names(names: list[str]) -> list[BinProvider]:
    providers: list[BinProvider] = []
    for name in names:
        provider_class = PROVIDER_CLASS_BY_NAME.get(name)
        if provider_class is not None:
            providers.append(provider_class())
    return providers


def _provider_for_name(provider_name: str, binary_name: str, overrides: dict[str, Any] | None) -> BinProvider | None:
    provider_class = PROVIDER_CLASS_BY_NAME.get(provider_name)
    if provider_class is None:
        return None
    provider = provider_class()
    provider_overrides = overrides.get(provider_name) if isinstance(overrides, dict) else None
    if isinstance(provider_overrides, dict):
        provider = provider.get_provider_with_overrides(
            overrides={binary_name: provider_overrides},
        )
    return provider
