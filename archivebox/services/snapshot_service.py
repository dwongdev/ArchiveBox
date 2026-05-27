from __future__ import annotations

from asgiref.sync import sync_to_async
from django.utils import timezone
from abx_dl.events import SnapshotCompletedEvent, SnapshotEvent
from abx_dl.limits import CrawlLimitState
from abx_dl.services.base import BaseService


class SnapshotService(BaseService):
    LISTENS_TO = [SnapshotEvent, SnapshotCompletedEvent]
    EMITS = []

    def __init__(self, bus, *, crawl_id: str, schedule_snapshot):
        self.crawl_id = crawl_id
        self.schedule_snapshot = schedule_snapshot
        super().__init__(bus)
        self.bus.on(SnapshotEvent, self.on_SnapshotEvent)
        self.bus.on(SnapshotCompletedEvent, self.on_SnapshotCompletedEvent)

    async def on_SnapshotEvent(self, event: SnapshotEvent) -> None:
        from archivebox.core.models import Snapshot

        snapshot = await Snapshot.objects.filter(id=event.snapshot_id, crawl_id=self.crawl_id).afirst()

        if snapshot is not None:
            if snapshot.status == Snapshot.StatusChoices.QUEUED:
                await sync_to_async(snapshot.sm.tick, thread_sensitive=True)()
                await sync_to_async(snapshot.refresh_from_db, thread_sensitive=True)()
            elif snapshot.status != Snapshot.StatusChoices.STARTED:
                return
            if snapshot.status != Snapshot.StatusChoices.STARTED:
                return
            await sync_to_async(snapshot.ensure_crawl_symlink, thread_sensitive=True)()

    async def on_SnapshotCompletedEvent(self, event: SnapshotCompletedEvent) -> None:
        from archivebox.core.models import Snapshot

        snapshot = await Snapshot.objects.select_related("crawl", "crawl__created_by").filter(id=event.snapshot_id).afirst()
        snapshot_id: str | None = None
        if snapshot is not None:
            snapshot.downloaded_at = snapshot.downloaded_at or timezone.now()
            await snapshot.asave(update_fields=["downloaded_at", "modified_at"])
            stop_reason = await sync_to_async(self._crawl_limit_stop_reason, thread_sensitive=True)(snapshot.crawl)
            if snapshot.crawl_id and stop_reason == "crawl_max_size":
                await (
                    Snapshot.objects.filter(
                        crawl_id=snapshot.crawl_id,
                        status=Snapshot.StatusChoices.QUEUED,
                    )
                    .exclude(id=snapshot.id)
                    .aupdate(
                        status=Snapshot.StatusChoices.SEALED,
                        retry_at=None,
                        modified_at=timezone.now(),
                    )
                )
            if snapshot.status == Snapshot.StatusChoices.QUEUED:
                await sync_to_async(snapshot.sm.tick, thread_sensitive=True)()
                await sync_to_async(snapshot.refresh_from_db, thread_sensitive=True)()
            if snapshot.status == Snapshot.StatusChoices.STARTED:
                await sync_to_async(snapshot.sm.seal, thread_sensitive=True)()
            snapshot_id = str(snapshot.id)
        if snapshot_id:
            snapshot = await Snapshot.objects.filter(id=snapshot_id).select_related("crawl", "crawl__created_by").afirst()
            if snapshot is not None:
                await sync_to_async(snapshot.write_index_jsonl, thread_sensitive=True)()
                await sync_to_async(snapshot.write_json_details, thread_sensitive=True)()
                await sync_to_async(snapshot.write_html_details, thread_sensitive=True)()

    def _crawl_limit_stop_reason(self, crawl) -> str:
        from archivebox.config.common import get_config

        config = get_config(crawl=crawl, include_machine=False)
        config["CRAWL_DIR"] = str(crawl.output_dir)
        return CrawlLimitState.from_config(config).get_stop_reason()
