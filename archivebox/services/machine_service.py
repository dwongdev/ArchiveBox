from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from abx_dl.events import MachineEvent
from abx_dl.services.base import BaseService

_BINARY_EVENT_ALLOWED_KEYS = frozenset({"ABX_INSTALL_CACHE"})


def _is_binary_event_key(key: str) -> bool:
    """``MachineEvent`` projector only ever writes binary-related state.

    ``Machine.config`` mirrors ``ArchiveBox.conf`` so arbitrary user keys can
    legitimately live there — but they get there through the file ↔ DB sync,
    not through events. Letting events write arbitrary keys would let an
    untrusted plugin overwrite security-sensitive user config (the file ↔ DB
    mirror is a security boundary), so the projector strips anything that
    isn't a binary path or the binary install cache.
    """
    if key in _BINARY_EVENT_ALLOWED_KEYS:
        return True
    return key.endswith("_BINARY")


def _strip_to_binary_keys(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    return {key: value for key, value in config.items() if _is_binary_event_key(str(key))}


class MachineService(BaseService):
    LISTENS_TO = [MachineEvent]
    EMITS = []

    def __init__(self, bus):
        super().__init__(bus)
        self.bus.on(MachineEvent, self.on_MachineEvent__save_to_db)

    async def on_MachineEvent__save_to_db(self, event: MachineEvent) -> None:
        from archivebox.machine.models import Machine, _sanitize_machine_config
        from archivebox.config.common import get_config

        if event.config_type != "derived":
            return

        machine = await sync_to_async(Machine.current, thread_sensitive=True)()
        lib_dir = await sync_to_async(lambda: get_config(include_machine=False).LIB_DIR, thread_sensitive=True)()
        config = dict(machine.config or {})

        if event.config is not None:
            binary_only = _strip_to_binary_keys(event.config)
            config.update(_sanitize_machine_config(binary_only, lib_dir=lib_dir))
        elif event.method == "update":
            key = event.key.replace("config/", "", 1).strip()
            if key and _is_binary_event_key(key):
                config[key] = event.value
        elif event.method == "unset":
            key = event.key.replace("config/", "", 1).strip()
            if key and _is_binary_event_key(key):
                config.pop(key, None)
        else:
            return

        machine.config = _sanitize_machine_config(config, lib_dir=lib_dir)
        await machine.asave(update_fields=["config", "modified_at"])
