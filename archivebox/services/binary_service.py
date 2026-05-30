from __future__ import annotations

from pathlib import Path

from asgiref.sync import sync_to_async

from abx_dl.events import BinaryRequestEvent, BinaryEvent
from abx_dl.services.base import BaseService


class BinaryService(BaseService):
    LISTENS_TO = [BinaryRequestEvent, BinaryEvent]
    EMITS = []

    def __init__(self, bus):
        super().__init__(bus)
        self.bus.on(BinaryRequestEvent, self.on_BinaryRequestEvent)
        self.bus.on(BinaryEvent, self.on_BinaryEvent)

    async def on_BinaryRequestEvent(self, event: BinaryRequestEvent) -> str | None:
        from archivebox.machine.models import Binary, Machine, _canonical_binary_name

        machine = await sync_to_async(Machine.current, thread_sensitive=True)()
        binary_name = _canonical_binary_name(event.name)
        if not binary_name:
            return None
        existing = await Binary.objects.filter(machine=machine, name=binary_name).afirst()
        cache_invalidated = False
        if existing and existing.status == Binary.StatusChoices.INSTALLED:
            changed = False
            if event.binproviders and existing.binproviders != event.binproviders:
                existing.binproviders = event.binproviders
                changed = True
            if event.overrides and existing.overrides != event.overrides:
                existing.overrides = event.overrides
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
                binproviders=event.binproviders,
                overrides=event.overrides or {},
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
            if installed is not None and event.overrides and installed.overrides != event.overrides:
                installed.status = Binary.StatusChoices.QUEUED
                installed.retry_at = None
                await installed.asave(update_fields=["status", "retry_at", "modified_at"])
                installed = None
        cached = None
        if installed is not None:
            from archivebox.config.common import get_config
            from abxpkg import BinProvider, PROVIDER_CLASS_BY_NAME

            binary_env: dict[str, str] = {}
            installed_path = Path(installed.abspath).expanduser().resolve(strict=False)
            active_lib_dir = (
                Path(str((await sync_to_async(get_config, thread_sensitive=True)()).get("LIB_DIR", "")))
                .expanduser()
                .resolve(
                    strict=False,
                )
            )
            provider_name = (installed.binprovider or installed.binproviders.split(",", 1)[0]).strip()
            if active_lib_dir and provider_name in {"npm", "pip", "puppeteer", "uv", "deno", "gem", "cargo", "goget", "nix", "bash"}:
                try:
                    installed_path.relative_to(active_lib_dir)
                except ValueError:
                    installed.status = Binary.StatusChoices.QUEUED
                    installed.retry_at = None
                    await installed.asave(update_fields=["status", "retry_at", "modified_at"])
                    installed = None
            if installed is None:
                return None
            provider_class = PROVIDER_CLASS_BY_NAME.get(provider_name)
            if provider_class is not None:
                provider = provider_class()
                overrides = installed.overrides if isinstance(installed.overrides, dict) else {}
                provider_overrides = overrides.get(provider_name)
                if isinstance(provider_overrides, dict):
                    provider = provider.get_provider_with_overrides(
                        overrides={installed.name: provider_overrides},
                    )
                binary_env = BinProvider.build_exec_env(
                    providers=[provider],
                    base_env={},
                )
            cached = {
                "abspath": installed.abspath,
                "version": installed.version or "",
                "sha256": installed.sha256 or "",
                "binproviders": installed.binproviders or "",
                "binprovider": installed.binprovider or "",
                "machine_id": str(installed.machine_id),
                "overrides": installed.overrides or {},
                "env": binary_env,
            }
        if cached is not None:
            binary_event = BinaryEvent(
                name=event.name,
                plugin_name=event.plugin_name,
                hook_name=event.hook_name,
                abspath=cached["abspath"],
                version=cached["version"],
                sha256=cached["sha256"],
                binproviders=event.binproviders or cached["binproviders"],
                binprovider=cached["binprovider"],
                overrides=event.overrides or cached["overrides"],
                env=cached["env"],
                binary_id=event.binary_id,
                machine_id=cached["machine_id"],
            )
            await event.emit(binary_event).now()
            return binary_event.abspath
        return None

    async def on_BinaryEvent(self, event: BinaryEvent) -> None:
        from archivebox.machine.models import Binary, Machine, _canonical_binary_name
        from archivebox.config.common import get_config

        machine = await sync_to_async(Machine.current, thread_sensitive=True)()
        binary_name = _canonical_binary_name(event.name)
        if not binary_name:
            return
        binary, _ = await Binary.objects.aget_or_create(
            machine=machine,
            name=binary_name,
            defaults={
                "status": Binary.StatusChoices.QUEUED,
            },
        )
        binary.abspath = event.abspath
        if event.version:
            binary.version = event.version
        if event.sha256:
            binary.sha256 = event.sha256
        if event.binproviders:
            binary.binproviders = event.binproviders
        if event.binprovider:
            binary.binprovider = event.binprovider
        if event.overrides and binary.overrides != event.overrides:
            binary.overrides = event.overrides
        binary.status = Binary.StatusChoices.INSTALLED
        binary.retry_at = None
        await binary.asave(
            update_fields=["abspath", "version", "sha256", "binproviders", "binprovider", "overrides", "status", "retry_at", "modified_at"],
        )
        lib_bin_dir = await sync_to_async(lambda: get_config().LIB_BIN_DIR, thread_sensitive=True)()
        await sync_to_async(binary.symlink_to_lib_bin_after_commit, thread_sensitive=True)(lib_bin_dir)
