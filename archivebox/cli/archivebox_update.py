#!/usr/bin/env python3
from __future__ import annotations

__package__ = "archivebox.cli"

import os
import asyncio
import shlex
import time

from typing import TYPE_CHECKING, Any
from collections.abc import Iterable
from pathlib import Path

import rich_click as click

from archivebox.misc.util import enforce_types, docstring

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from archivebox.core.models import Snapshot
    from archivebox.crawls.models import Crawl


def _get_snapshot_crawl(snapshot: Snapshot) -> Crawl | None:
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return snapshot.crawl
    except ObjectDoesNotExist:
        return None


def _get_search_indexing_plugins() -> list[str]:
    from abx_dl.models import discover_plugins
    from archivebox.hooks import get_search_backends

    available_backends = set(get_search_backends())
    plugins = discover_plugins()
    return sorted(
        plugin_name
        for plugin_name, plugin in plugins.items()
        if plugin_name.startswith("search_backend_")
        and plugin_name.removeprefix("search_backend_") in available_backends
        and any("Snapshot" in hook.name and "index" in hook.name.lower() for hook in plugin.hooks)
    )


def _build_filtered_snapshots_queryset(
    *,
    filter_patterns: Iterable[str],
    filter_type: str,
    status: str | None = None,
    url__icontains: str | None = None,
    url__istartswith: str | None = None,
    tag: str | None = None,
    crawl_id: str | None = None,
    limit: int | None = None,
    sort: str | None = None,
    search: str | None = None,
    before: float | None = None,
    after: float | None = None,
    resume: str | None = None,
):
    from datetime import datetime
    from archivebox.cli.archivebox_snapshot import build_snapshot_queryset

    filter_patterns = tuple(filter_patterns)
    snapshots = build_snapshot_queryset(
        status=status,
        url__icontains=url__icontains,
        url__istartswith=url__istartswith,
        tag=tag,
        crawl_id=crawl_id,
        sort=sort,
        search=search,
        query=" ".join(filter_patterns) if search else None,
    )

    if filter_patterns and not search:
        snapshots = snapshots.filter_by_patterns(list(filter_patterns), filter_type)

    if before:
        snapshots = snapshots.filter(bookmarked_at__lt=datetime.fromtimestamp(before))
    if after:
        snapshots = snapshots.filter(bookmarked_at__gt=datetime.fromtimestamp(after))
    if resume:
        snapshots = snapshots.filter(timestamp__lte=resume)
    if not sort:
        snapshots = snapshots.order_by("-timestamp")
    snapshots = snapshots.select_related("crawl")
    if limit:
        limited_ids = list(snapshots.values_list("id", flat=True)[:limit])
        snapshots = snapshots.model.objects.filter(id__in=limited_ids).select_related("crawl")

    return snapshots


def reindex_snapshots(
    snapshots: QuerySet[Snapshot, Snapshot],
    *,
    search_plugins: list[str],
    batch_size: int,
    collect_ids: bool = False,
    wait_for_turn=None,
) -> dict[str, Any]:
    from archivebox.cli.archivebox_extract import run_plugins

    stats: dict[str, Any] = {"processed": 0, "queued": 0, "reindexed": 0, "snapshot_ids": []}
    records: list[dict[str, str]] = []

    total = snapshots.count()
    print(f"[*] Reindexing {total} snapshots with search plugins: {', '.join(search_plugins)}")

    def run_batch() -> None:
        if not records:
            return
        if wait_for_turn:
            wait_for_turn()
        batch_records = list(records)
        # Index-only backfill intentionally queues only search ArchiveResult
        # rows. The extract runner bumps Snapshot.retry_at so the orchestrator
        # sees the maintenance work, but it does not change status away from
        # PAUSED; run_due_snapshot restores retry_at=MAX after the targeted
        # plugin rows finish.
        exit_code = run_plugins(
            args=(),
            records=batch_records,
            wait=False,
            emit_results=False,
            show_progress=False,
        )
        if exit_code != 0:
            raise SystemExit(exit_code)
        print(
            f"    [{stats['processed']}/{total}] Queued {len(batch_records)} index jobs for orchestrator",
        )
        records.clear()

    for snapshot in snapshots.select_related("crawl").paged_iterator(chunk_size=batch_size):
        try:
            stats["processed"] += 1

            if _get_snapshot_crawl(snapshot) is None:
                continue

            if collect_ids:
                stats["snapshot_ids"].append(str(snapshot.id))
            for plugin_name in search_plugins:
                records.append(
                    {
                        "type": "ArchiveResult",
                        "snapshot_id": str(snapshot.id),
                        "plugin": plugin_name,
                    },
                )
                stats["queued"] += 1
            if len(records) >= batch_size:
                run_batch()
        except KeyboardInterrupt as err:
            err.archivebox_resume = snapshot.timestamp
            raise

    run_batch()
    return stats


@enforce_types
def update(
    filter_patterns: Iterable[str] = (),
    filter_type: str = "exact",
    status: str | None = None,
    url__icontains: str | None = None,
    url__istartswith: str | None = None,
    tag: str | None = None,
    crawl_id: str | None = None,
    limit: int | None = None,
    sort: str | None = None,
    search: str | None = None,
    before: float | None = None,
    after: float | None = None,
    resume: str | None = None,
    batch_size: int = 100,
    continuous: bool = False,
    index_only: bool = False,
    migrate_only: bool = False,
    stop_daemon_stack: bool = True,
) -> None:
    """
    Update snapshots: migrate old dirs, reconcile DB, and re-queue for archiving.

    Three-phase operation (without filters):
    - Phase 1: Drain old archive/ dirs by moving to new fs location (0.8.x → 0.9.x)
    - Phase 2: O(n) scan over entire DB from most recent to least recent
    - No orphan scans needed (trust 1:1 mapping between DB and filesystem after phase 1)

    With filters: Only phase 2 (DB query), no filesystem operations.
    Without filters: All phases (full update).
    """

    from rich import print
    from archivebox.config import CONSTANTS
    from archivebox.config.django import setup_django

    setup_django()
    from archivebox.misc.checks import check_migrations

    # This must be the first database operation in `archivebox update`.
    # Old 0.7.x/0.8.x collections may not have current machine/process/crawl
    # tables yet, and even "harmless" runtime-stack bookkeeping uses current
    # ORM models. Apply Django migrations before creating Process rows, checking
    # runtime ownership, queuing retry_at maintenance ticks, or touching any
    # lazy Snapshot.save() filesystem migration path.
    print("[*] Checking for pending migrations...")
    check_migrations(auto_apply=True)

    from archivebox.machine.models import Process
    from archivebox.core.shutdown_util import foreground_parent_watchdog, foreground_shutdown_signals
    from archivebox.services.supervision_service import (
        command_owns_runtime_stack,
        current_command,
        ensure_daemon_stack,
        standby_until_runtime_stack_needed,
    )
    from archivebox.workers.supervisord_util import run_runner_worker, stop_existing_supervisord_process, stop_own_supervisord_process

    command = current_command(Process.TypeChoices.UPDATE, data_dir=CONSTANTS.DATA_DIR)

    def wait_for_turn() -> None:
        standby_until_runtime_stack_needed(command, data_dir=CONSTANTS.DATA_DIR)

    def run_scoped_runner(*args: str) -> None:
        while True:
            wait_for_turn()
            exit_code = run_runner_worker(list(args), name=f"worker_runner_update_{os.getpid()}")
            if exit_code == 0:
                return
            if not command_owns_runtime_stack(command, data_dir=CONSTANTS.DATA_DIR):
                continue
            raise SystemExit(exit_code)

    is_filtered_update = any(
        (
            filter_patterns,
            status,
            url__icontains,
            url__istartswith,
            tag,
            crawl_id,
            limit,
            sort,
            search,
            before,
            after,
        ),
    )
    touched_snapshot_ids: set[str] = set()
    exit_code = 0

    try:
        wait_for_turn()
        if stop_daemon_stack:
            stop_existing_supervisord_process()

        with foreground_shutdown_signals(), foreground_parent_watchdog():
            while True:
                do_migrate = migrate_only or not index_only
                do_index = index_only or not migrate_only
                do_run_until_idle = do_migrate or do_index
                ran_post_migrate_runner = False

                if do_migrate:
                    if (
                        filter_patterns
                        or status
                        or url__icontains
                        or url__istartswith
                        or tag
                        or crawl_id
                        or limit
                        or sort
                        or search
                        or before
                        or after
                    ):
                        print("[*] Processing filtered snapshots from database...")
                        stats = process_filtered_snapshots(
                            filter_patterns=filter_patterns,
                            filter_type=filter_type,
                            status=status,
                            url__icontains=url__icontains,
                            url__istartswith=url__istartswith,
                            tag=tag,
                            crawl_id=crawl_id,
                            limit=limit,
                            sort=sort,
                            search=search,
                            before=before,
                            after=after,
                            resume=resume,
                            batch_size=batch_size,
                            queue_for_archiving=do_run_until_idle,
                            wait_for_turn=wait_for_turn,
                        )
                        print_stats(stats)
                        touched_snapshot_ids.update(stats.get("snapshot_ids", []))
                    else:
                        stats_combined = {"phase1": {}, "phase2": {}}

                        print("[*] Phase 1: Draining old archive/ directories (0.8.x → 0.9.x migration)...")
                        stats_combined["phase1"] = drain_old_archive_dirs(
                            resume_from=resume,
                            batch_size=batch_size,
                        )

                        print("[*] Phase 2: Processing all database snapshots (most recent first)...")
                        stats_combined["phase2"] = process_all_db_snapshots(
                            batch_size=batch_size,
                            resume=resume,
                            wait_for_turn=wait_for_turn,
                        )
                        print_combined_stats(stats_combined)

                    if do_run_until_idle:
                        # Filesystem migration is maintenance on existing
                        # Snapshot rows: Snapshot.save() moves archive/<ts> to
                        # the current output_dir and preserves the lifecycle
                        # status. Drain those retry_at ticks before queuing
                        # search backfill below. Otherwise the sealed/paused
                        # runner branch correctly sees queued ArchiveResult
                        # rows first, runs the targeted plugins, and may leave
                        # the fs_version maintenance tick hidden behind that
                        # plugin work until another update pass.
                        print("[*] Phase 3: Running filesystem maintenance until idle...")
                        if is_filtered_update:
                            if not touched_snapshot_ids:
                                print("[*] No matching snapshots queued work for the runner.")
                            for snapshot_id in sorted(touched_snapshot_ids):
                                run_scoped_runner("--snapshot-id", snapshot_id)
                        else:
                            run_scoped_runner("--maintenance-only")
                        ran_post_migrate_runner = True

                if do_index:
                    ensure_daemon_stack(reason="search indexing")
                    search_plugins = _get_search_indexing_plugins()
                    if not search_plugins:
                        print("[*] No search indexing plugins are available, nothing to backfill.")
                    else:
                        snapshots = _build_filtered_snapshots_queryset(
                            filter_patterns=filter_patterns,
                            filter_type=filter_type,
                            status=status,
                            url__icontains=url__icontains,
                            url__istartswith=url__istartswith,
                            tag=tag,
                            crawl_id=crawl_id,
                            limit=limit,
                            sort=sort,
                            search=search,
                            before=before,
                            after=after,
                            resume=resume,
                        )
                        stats = reindex_snapshots(
                            snapshots,
                            search_plugins=search_plugins,
                            batch_size=batch_size,
                            collect_ids=is_filtered_update,
                            wait_for_turn=wait_for_turn,
                        )
                        print_index_stats(stats)
                        touched_snapshot_ids.update(stats.get("snapshot_ids", []))

                if do_run_until_idle and (do_index or not ran_post_migrate_runner):
                    # Search/index backfill intentionally queues targeted
                    # ArchiveResult rows without reopening sealed/paused
                    # snapshots. This second runner pass drains those plugin
                    # rows after filesystem maintenance has had its own turn.
                    # For a normal unfiltered `archivebox update`, keep the
                    # historical final pass broad enough to resume genuinely
                    # queued/interrupted crawl work after maintenance is done.
                    print("[*] Phase 3: Running queued/interrupted crawl work until idle...")
                    if is_filtered_update:
                        if not touched_snapshot_ids:
                            print("[*] No matching snapshots queued work for the runner.")
                        for snapshot_id in sorted(touched_snapshot_ids):
                            run_scoped_runner("--snapshot-id", snapshot_id)
                    else:
                        run_scoped_runner(*(["--maintenance-only"] if index_only or migrate_only else []))

                if not continuous:
                    break

                print("[yellow]Sleeping 60s before next pass...[/yellow]")
                time.sleep(60)
                resume = None
    except (KeyboardInterrupt, asyncio.CancelledError) as err:
        exit_code = 130
        exact_resume = getattr(err, "archivebox_resume", None)
        resume_cmd = ["archivebox", "update"]
        if migrate_only:
            resume_cmd.append("--migrate-only")
        if index_only:
            resume_cmd.append("--index-only")
        if batch_size != 100:
            resume_cmd.extend(["--batch-size", str(batch_size)])
        if exact_resume or resume:
            resume_cmd.extend(["--resume", str(exact_resume or resume)])
        if before is not None:
            resume_cmd.extend(["--before", str(before)])
        if after is not None:
            resume_cmd.extend(["--after", str(after)])
        if filter_type != "exact":
            resume_cmd.extend(["--filter-type", filter_type])
        if status:
            resume_cmd.extend(["--status", status])
        if url__icontains:
            resume_cmd.extend(["--url__icontains", url__icontains])
        if url__istartswith:
            resume_cmd.extend(["--url__istartswith", url__istartswith])
        if tag:
            resume_cmd.extend(["--tag", tag])
        if crawl_id:
            resume_cmd.extend(["--crawl-id", crawl_id])
        if limit:
            resume_cmd.extend(["--limit", str(limit)])
        if sort:
            resume_cmd.extend(["--sort", sort])
        if search:
            resume_cmd.extend(["--search", search])
        resume_cmd.extend(str(pattern) for pattern in filter_patterns)
        print("\n[red][X] archivebox update interrupted.[/red]")
        print("[yellow]Hint: resume this idempotent update with:[/yellow]")
        print(f"    [green]{shlex.join(resume_cmd)}[/green]")
        raise SystemExit(exit_code)
    except SystemExit as err:
        if isinstance(err.code, int):
            exit_code = err.code
        elif err.code:
            exit_code = 1
        raise
    finally:
        command.mark_exited(exit_code=exit_code)
        if stop_daemon_stack:
            stop_own_supervisord_process()


def drain_old_archive_dirs(resume_from: str | None = None, batch_size: int = 100) -> dict[str, int]:
    """
    Drain old archive/ directories (0.8.x → 0.9.x migration).

    Only processes real directories (skips symlinks - those are already migrated).
    For each old dir found in archive/:
      1. Load or create DB snapshot
      2. Trigger fs migration on save() to move to data/archive/users/{user}/...
      3. Leave symlink in archive/ pointing to new location

    After this drains, archive/ should only contain symlinks and we can trust
    1:1 mapping between DB and filesystem.
    """
    from archivebox.core.models import Snapshot
    from archivebox.config.common import get_config
    from archivebox.crawls.models import Crawl
    from django.utils import timezone

    stats = {"processed": 0, "migrated": 0, "queued": 0, "skipped": 0, "invalid": 0}
    crawl_url_lines: dict[str, list[str]] = {}
    crawl_url_sets: dict[str, set[str]] = {}
    dirty_crawl_ids: set[str] = set()

    runtime_config = get_config()
    archive_dir = runtime_config.ARCHIVE_DIR
    if not archive_dir.exists():
        return stats

    last_crawl_id = None
    while True:
        crawl_qs = Crawl.objects.filter(label__startswith="[migration] orphaned").order_by("id")
        if last_crawl_id is not None:
            crawl_qs = crawl_qs.filter(id__gt=last_crawl_id)
        crawl_batch = list(crawl_qs[:batch_size])
        if not crawl_batch:
            break
        for crawl in crawl_batch:
            last_crawl_id = crawl.id
            url_entries = crawl._iter_url_lines()
            existing_urls = {url for _raw_line, url in url_entries if url}
            lines = (crawl.urls or "").splitlines()
            changed = False
            for url in crawl.snapshot_set.order_by("timestamp").values_list("url", flat=True):
                if url not in existing_urls:
                    lines.append(url)
                    existing_urls.add(url)
                    changed = True
            if changed:
                Crawl.objects.filter(pk=crawl.pk).update(urls="\n".join(lines), modified_at=timezone.now())

    # Scan for real directories only (skip symlinks - they're already migrated)
    all_entries = list(os.scandir(archive_dir))
    entries = [
        (e.stat().st_mtime, e.path)
        for e in all_entries
        if e.is_dir(follow_symlinks=False) and Snapshot.is_legacy_archive_dir(Path(e.path))  # Skip symlinks and 0.9.x roots
    ]
    entries.sort(reverse=True)  # Newest first
    print(f"[*] Found {len(entries)} old directories to drain")

    for mtime, entry_path in entries:
        entry_path = Path(entry_path)

        # Resume from timestamp if specified
        if resume_from and entry_path.name > resume_from:
            continue

        stats["processed"] += 1

        # Try to load existing snapshot from DB
        snapshot = Snapshot.load_from_directory(entry_path)

        if not snapshot:
            # Not in DB - create new snapshot record
            snapshot = Snapshot.create_from_directory(entry_path)
            if not snapshot:
                # Invalid directory - move to invalid/
                Snapshot.move_directory_to_invalid(entry_path)
                stats["invalid"] += 1
                print(f"    [{stats['processed']}] Invalid: {entry_path.name}")
                continue

            try:
                snapshot.status = Snapshot.StatusChoices.SEALED
                snapshot.retry_at = timezone.now()
                Snapshot.objects.bulk_create([snapshot])
                Snapshot.objects.filter(pk=snapshot.pk).update(
                    status=Snapshot.StatusChoices.SEALED,
                    retry_at=snapshot.retry_at,
                )

                crawl = _get_snapshot_crawl(snapshot)
                if crawl is not None:
                    crawl_cache_key = str(crawl.id)
                    existing_urls = crawl_url_sets.get(crawl_cache_key)
                    if existing_urls is None:
                        url_entries = crawl._iter_url_lines()
                        existing_urls = {url for _raw_line, url in url_entries if url}
                        crawl_url_sets[crawl_cache_key] = existing_urls
                        crawl_url_lines[crawl_cache_key] = (crawl.urls or "").splitlines()
                    if snapshot.url not in existing_urls:
                        crawl_url_lines[crawl_cache_key].append(snapshot.url)
                        existing_urls.add(snapshot.url)
                        dirty_crawl_ids.add(crawl_cache_key)

                stats["queued"] += 1
                print(f"    [{stats['processed']}] Imported orphaned snapshot and queued migration: {entry_path.name}")
            except Exception as e:
                stats["skipped"] += 1
                print(f"    [{stats['processed']}] Skipped (error: {e}): {entry_path.name}")
            continue

        # Ensure snapshot has a valid crawl (migration 0024 may have failed)
        has_valid_crawl = _get_snapshot_crawl(snapshot) is not None

        if not has_valid_crawl:
            # Create a new crawl (created_by will default to system user)
            crawl = Crawl.objects.create(urls=snapshot.url)
            # Use queryset update to avoid save() hooks and keep the SQLite
            # write to one statement while the migration loop does filesystem
            # work outside any transaction.
            from archivebox.core.models import Snapshot as SnapshotModel

            SnapshotModel.objects.filter(pk=snapshot.pk).update(crawl=crawl)
            # Refresh the instance
            snapshot.crawl = crawl

        # Check if needs migration (0.8.x → 0.9.x)
        try:
            if snapshot.fs_migration_needed:
                Snapshot.objects.filter(pk=snapshot.pk).update(
                    retry_at=timezone.now(),
                    modified_at=timezone.now(),
                )
                stats["queued"] += 1
                print(f"    [{stats['processed']}] Queued filesystem migration: {entry_path.name}")
            else:
                stats["skipped"] += 1
        except Exception as e:
            stats["skipped"] += 1
            print(f"    [{stats['processed']}] Skipped (error: {e}): {entry_path.name}")

        if stats["processed"] % batch_size == 0:
            for crawl_id in tuple(dirty_crawl_ids):
                Crawl.objects.filter(pk=crawl_id).update(
                    urls="\n".join(crawl_url_lines[crawl_id]),
                    modified_at=timezone.now(),
                )
            dirty_crawl_ids.clear()

    for crawl_id in tuple(dirty_crawl_ids):
        Crawl.objects.filter(pk=crawl_id).update(
            urls="\n".join(crawl_url_lines[crawl_id]),
            modified_at=timezone.now(),
        )
    dirty_crawl_ids.clear()
    return stats


def process_all_db_snapshots(batch_size: int = 100, resume: str | None = None, wait_for_turn=None) -> dict[str, int]:
    """
    O(n) scan over entire DB from most recent to least recent.

    For each snapshot:
      1. Reconcile index.json with DB (merge titles, tags, archive results)
      2. Mark migrated snapshots sealed unless explicitly re-queued elsewhere

    No orphan detection needed - we trust 1:1 mapping between DB and filesystem
    after Phase 1 has drained all old archive/ directories.
    """
    import uuid
    from archivebox.core.models import Snapshot
    from archivebox.crawls.models import Crawl
    from django.utils import timezone

    stats = {
        "processed": 0,
        "scanned_dirs": 0,
        "updated_json": 0,
        "updated_db": 0,
        "queued": 0,
        "sealed": 0,
        "crawls_queued": 0,
    }
    current_fs_version = Snapshot._fs_current_version()

    queryset = Snapshot.objects.all()
    if resume:
        queryset = queryset.filter(timestamp__lte=resume)
    total = queryset.count()
    print(f"[*] Processing {total} snapshots from database (most recent first)...")

    def update_in_batches(rows, *, label: str, **updates) -> int:
        updated = 0
        checked = 0
        while True:
            if wait_for_turn:
                wait_for_turn()
            ids = list(rows.order_by("-timestamp").values_list("id", flat=True)[:batch_size])
            if not ids:
                if updated:
                    print(f"    [{label}] complete: {updated} rows updated")
                return updated
            checked += len(ids)
            print(f"    [{label}] updating next {len(ids)} rows (seen {checked})...")
            # Each batch is one short UPDATE and is intentionally idempotent.
            # If the command is interrupted, these rows no longer match on the
            # next run and remaining rows continue from DB state.
            updated += Snapshot.objects.filter(id__in=ids).update(**updates)
            print(f"    [{label}] updated {updated} rows so far")

    now = timezone.now()
    updated_rows = update_in_batches(
        queryset.exclude(
            status__in=[
                Snapshot.StatusChoices.QUEUED,
                Snapshot.StatusChoices.STARTED,
                Snapshot.StatusChoices.PAUSED,
                Snapshot.StatusChoices.SEALED,
            ],
        ),
        label="snapshot status normalization",
        status=Snapshot.StatusChoices.SEALED,
        retry_at=None,
        modified_at=now,
    )
    stats["sealed"] += updated_rows
    stats["updated_db"] += updated_rows

    fs_version_rows = queryset.exclude(fs_version=current_fs_version)
    stale_batch: list[tuple[uuid.UUID, uuid.UUID | None, str]] = []

    def queue_stale_fs_batch() -> None:
        if not stale_batch:
            return
        if wait_for_turn:
            wait_for_turn()
        now = timezone.now()
        snapshot_ids = [snapshot_id for snapshot_id, _crawl_id, _timestamp in stale_batch]
        # Do not bump fs_version here. The orchestrator calls Snapshot.save(),
        # which performs the idempotent filesystem migration and commits the new
        # fs_version in the same serialized worker path as normal crawls.
        updated = Snapshot.objects.filter(id__in=snapshot_ids).update(
            retry_at=now,
            modified_at=now,
        )
        stats["processed"] += len(stale_batch)
        stats["updated_db"] += updated
        stats["queued"] += updated
        print(f"    [{stats['processed']}/{total}] Queued {updated} filesystem migrations for orchestrator...")
        stale_batch.clear()

    for snapshot in fs_version_rows.only("id", "crawl_id", "timestamp").order_by("-timestamp").paged_iterator(chunk_size=batch_size):
        try:
            stale_batch.append((snapshot.id, snapshot.crawl_id, snapshot.timestamp))
            if len(stale_batch) >= batch_size:
                queue_stale_fs_batch()
        except KeyboardInterrupt as err:
            err.archivebox_resume = snapshot.timestamp
            raise
    queue_stale_fs_batch()

    now = timezone.now()
    stats["crawls_queued"] = (
        Crawl.objects.filter(
            status__in=[Crawl.StatusChoices.QUEUED, Crawl.StatusChoices.STARTED],
        )
        .exclude(
            snapshot_set__status__in=[
                Snapshot.StatusChoices.QUEUED,
                Snapshot.StatusChoices.STARTED,
                Snapshot.StatusChoices.PAUSED,
            ],
        )
        .update(
            retry_at=now,
            modified_at=now,
        )
    )
    stats["updated_db"] += stats["crawls_queued"]
    return stats


def process_filtered_snapshots(
    filter_patterns: Iterable[str],
    filter_type: str,
    status: str | None,
    url__icontains: str | None,
    url__istartswith: str | None,
    tag: str | None,
    crawl_id: str | None,
    limit: int | None,
    sort: str | None,
    search: str | None,
    before: float | None,
    after: float | None,
    resume: str | None,
    batch_size: int,
    queue_for_archiving: bool = True,
    wait_for_turn=None,
) -> dict[str, Any]:
    """Process snapshots matching filters (DB query only)."""
    from archivebox.core.models import Snapshot
    from django.utils import timezone

    stats: dict[str, Any] = {"processed": 0, "updated_json": 0, "updated_db": 0, "queued": 0, "snapshot_ids": []}

    snapshots = _build_filtered_snapshots_queryset(
        filter_patterns=filter_patterns,
        filter_type=filter_type,
        status=status,
        url__icontains=url__icontains,
        url__istartswith=url__istartswith,
        tag=tag,
        crawl_id=crawl_id,
        limit=limit,
        sort=sort,
        search=search,
        before=before,
        after=after,
        resume=resume,
    )

    total = snapshots.count()
    print(f"[*] Found {total} matching snapshots")

    for snapshot in snapshots.select_related("crawl").paged_iterator(chunk_size=batch_size):
        if wait_for_turn and stats["processed"] % batch_size == 0:
            wait_for_turn()
        stats["processed"] += 1

        # Skip snapshots with missing crawl references
        if _get_snapshot_crawl(snapshot) is None:
            continue

        try:
            stats["snapshot_ids"].append(str(snapshot.id))
            update_values = {}
            if not isinstance(snapshot.current_step, int):
                update_values["current_step"] = 0
            if queue_for_archiving:
                update_values.update(
                    {
                        "status": Snapshot.StatusChoices.QUEUED,
                        "retry_at": timezone.now(),
                        "modified_at": timezone.now(),
                    },
                )
            if update_values:
                # update() is intentionally used instead of save(); save()
                # runs output-dir hooks, which must not happen while SQLite
                # is holding the write lock for this state change. Index-only
                # maintenance goes through reindex_snapshots/run_plugins instead
                # so paused snapshots keep status=paused while only their
                # targeted search ArchiveResult rows run.
                Snapshot.objects.filter(pk=snapshot.pk).update(**update_values)
                stats["updated_db"] += 1

            stats["queued"] += 1 if queue_for_archiving else 0
        except KeyboardInterrupt as err:
            err.archivebox_resume = snapshot.timestamp
            raise
        except Exception as e:
            # Skip snapshots that can't be processed
            print(f"    [!] Skipping snapshot {snapshot.id}: {e}")
            continue

        if stats["processed"] % batch_size == 0:
            print(f"    [{stats['processed']}/{total}] Processed...")

    return stats


def print_stats(stats: dict):
    """Print statistics for filtered mode."""
    from rich import print

    print(f"""
[green]Update Complete[/green]
  Scanned rows:     {stats["processed"]}
  Updated JSON:     {stats.get("updated_json", 0)}
  Updated DB rows:  {stats.get("updated_db", 0)}
  Queued snapshots: {stats["queued"]}
""")


def print_combined_stats(stats_combined: dict):
    """Print statistics for full mode."""
    from rich import print

    s1 = stats_combined["phase1"]
    s2 = stats_combined["phase2"]

    print(f"""
[green]Archive Update Complete[/green]

Phase 1 (Drain Old Dirs):
  Scanned dirs:     {s1.get("processed", 0)}
  Moved files:      {s1.get("migrated", 0)}
  Skipped dirs:     {s1.get("skipped", 0)}
  Invalid dirs:     {s1.get("invalid", 0)}

Phase 2 (Process DB):
  Scanned dirs:     {s2.get("scanned_dirs", 0)}
  Updated JSON:     {s2.get("updated_json", 0)}
  Updated DB rows:  {s2.get("updated_db", 0)}
  Sealed snapshots: {s2.get("sealed", 0)}
  Queued crawls:    {s2.get("crawls_queued", 0)}
""")


def print_index_stats(stats: dict[str, Any]) -> None:
    from rich import print

    print(f"""
[green]Search Reindex Complete[/green]
  Scanned rows:      {stats["processed"]}
  Queued index jobs: {stats["queued"]}
""")


@click.command()
@click.option("--resume", type=str, help="Resume from timestamp")
@click.option("--status", "-s", help="Filter by status (queued, started, sealed)")
@click.option("--url__icontains", help="Filter by URL contains")
@click.option("--url__istartswith", help="Filter by URL starts with")
@click.option("--tag", "-t", help="Filter by tag name")
@click.option("--crawl-id", help="Filter by crawl ID")
@click.option("--limit", "-n", type=int, help="Limit number of snapshots to update")
@click.option("--sort", "-o", type=str, help="Field to sort by, e.g. url, created_at, bookmarked_at, downloaded_at")
@click.option("--search", type=click.Choice(["meta", "content", "contents", "deep"]), help="Search mode to use for positional query")
@click.option("--before", type=float, help="Only snapshots before timestamp")
@click.option("--after", type=float, help="Only snapshots after timestamp")
@click.option("--filter-type", type=click.Choice(["exact", "substring", "regex", "domain", "tag", "timestamp"]), default="exact")
@click.option("--batch-size", type=int, default=100, help="Commit every N snapshots")
@click.option("--continuous", is_flag=True, help="Run continuously as background worker")
@click.option("--index-only", is_flag=True, help="Backfill available search indexes from existing archived content")
@click.option("--migrate-only", is_flag=True, help="Only migrate filesystem and update database/index state")
@click.argument("filter_patterns", nargs=-1)
@docstring(update.__doc__)
def main(**kwargs):
    from archivebox.core.shutdown_util import foreground_parent_watchdog, foreground_shutdown_signals

    with foreground_shutdown_signals(), foreground_parent_watchdog():
        update(**kwargs)


if __name__ == "__main__":
    main()
