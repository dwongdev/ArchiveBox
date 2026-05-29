from __future__ import annotations

from django.utils import timezone
from rich.console import Console


def recover_orchestrator_state(*, include_chrome: bool = False) -> dict[str, int]:
    from archivebox.crawls.models import Crawl
    from archivebox.core.models import ArchiveResult, Snapshot
    from archivebox.machine.models import Process
    from django.db.models import Exists, OuterRef, Q, Subquery, Value
    from django.db.models.functions import Coalesce

    now = timezone.now()
    recovery_console = Console(stderr=True, highlight=False, soft_wrap=True)
    cleaned = {
        "processes_stale_running": Process.cleanup_stale_running(),
        "processes_orphaned_workers": Process.cleanup_orphaned_workers(),
        "chrome_processes_orphaned": Process.cleanup_orphaned_chrome() if include_chrome else 0,
        "crawls_queued_without_retry_at": 0,
        "snapshots_queued_without_retry_at": 0,
        "archiveresults_backoff": 0,
        "archiveresults_started_without_running_process": 0,
        "snapshots_started_without_running_results": 0,
        "crawls_started_with_due_snapshots": 0,
        "crawls_started_waiting_on_future_snapshots": 0,
        "crawls_started_without_active_snapshots": 0,
    }

    running_archiveresults = ArchiveResult.objects.filter(
        snapshot_id=OuterRef("pk"),
        status=ArchiveResult.StatusChoices.STARTED,
        process__status=Process.StatusChoices.RUNNING,
    )
    active_child_snapshots = Snapshot.objects.filter(
        crawl_id=OuterRef("pk"),
        status__in=[Snapshot.StatusChoices.QUEUED, Snapshot.StatusChoices.STARTED, Snapshot.StatusChoices.PAUSED],
    )
    due_child_snapshots = active_child_snapshots.exclude(status=Snapshot.StatusChoices.PAUSED).filter(
        Q(retry_at__isnull=True) | Q(retry_at__lte=now),
    )
    next_future_child_retry = Subquery(
        active_child_snapshots.filter(retry_at__gt=now).order_by("retry_at").values("retry_at")[:1],
    )

    # Broken lock repair: QUEUED rows with retry_at=NULL are invisible to the
    # queue. Set only the scheduling field so the runner owns the next tick.
    cleaned["crawls_queued_without_retry_at"] = Crawl.objects.filter(
        status=Crawl.StatusChoices.QUEUED,
        retry_at__isnull=True,
    ).update(retry_at=now, modified_at=now)
    cleaned["snapshots_queued_without_retry_at"] = Snapshot.objects.filter(
        status=Snapshot.StatusChoices.QUEUED,
        retry_at__isnull=True,
        crawl__status__in=[Crawl.StatusChoices.QUEUED, Crawl.StatusChoices.STARTED],
    ).update(retry_at=now, modified_at=now)
    # ArchiveResult has no retry_at scheduler; BACKOFF is a legacy/impossible
    # persisted state here, so move it back to QUEUED for the snapshot runner.
    cleaned["archiveresults_backoff"] = ArchiveResult.objects.filter(
        status=ArchiveResult.StatusChoices.BACKOFF,
    ).update(status=ArchiveResult.StatusChoices.QUEUED, modified_at=now)
    # Impossible state repair: STARTED ArchiveResults without a live Process
    # have no owner left to emit completion. Requeue only the result row; the
    # snapshot/crawl schedulers will pick up normal retry processing.
    cleaned["archiveresults_started_without_running_process"] = (
        ArchiveResult.objects.filter(status=ArchiveResult.StatusChoices.STARTED)
        .exclude(process__status=Process.StatusChoices.RUNNING)
        .update(status=ArchiveResult.StatusChoices.QUEUED, process=None, modified_at=now)
    )
    started_snapshots = Snapshot.objects.filter(status=Snapshot.StatusChoices.STARTED).filter(
        Q(retry_at__isnull=True) | Q(retry_at__gt=now),
    )

    # Broken lock repair: STARTED + retry_at=NULL or retry_at in the future
    # means "owned by an active runner". Recovery only runs from the current
    # elected runner after Process cleanup has proven old owners are gone, so
    # STARTED rows with no live ArchiveResult process should not wait out the
    # previous runner's full lease before the new runner can resume them.
    # We only unlock scheduling; normal Snapshot runner code owns the next
    # transition and side effects.
    cleaned["snapshots_started_without_running_results"] = (
        started_snapshots.annotate(has_running_results=Exists(running_archiveresults))
        .filter(has_running_results=False)
        .update(
            retry_at=now,
            modified_at=now,
        )
    )

    # Broken lock repair: STARTED + retry_at=NULL is an orphaned ownership
    # lease. Recovery only unlocks scheduling; the runner owns any subsequent
    # state-machine transition, including sealing rows whose children/results
    # are already final.
    recoverable_started_crawls = Crawl.objects.filter(status=Crawl.StatusChoices.STARTED).filter(
        Q(retry_at__isnull=True) | Q(retry_at__gt=now),
    )

    due_started_crawls = recoverable_started_crawls.annotate(has_due_child=Exists(due_child_snapshots)).filter(has_due_child=True)
    cleaned["crawls_started_with_due_snapshots"] = due_started_crawls.update(retry_at=now, modified_at=now)
    future_started_crawls = recoverable_started_crawls.annotate(
        has_active_child=Exists(active_child_snapshots),
        has_due_child=Exists(due_child_snapshots),
        next_child_retry=next_future_child_retry,
    ).filter(has_active_child=True, has_due_child=False)
    cleaned["crawls_started_waiting_on_future_snapshots"] = future_started_crawls.update(
        retry_at=Coalesce("next_child_retry", Value(now)),
        modified_at=now,
    )
    finished_started_crawls = recoverable_started_crawls.annotate(has_active_child=Exists(active_child_snapshots)).filter(
        has_active_child=False,
    )
    cleaned["crawls_started_without_active_snapshots"] = finished_started_crawls.update(retry_at=now, modified_at=now)

    repair_messages = {
        "processes_stale_running": (
            "Closing {count} interrupted process(es) "
            "(ArchiveBox may have been interrupted before it was able to record that they stopped; any affected work can now be retried)."
        ),
        "processes_orphaned_workers": (
            "Closing {count} interrupted extractor process(es) "
            "(ArchiveBox may have been interrupted before it was able to record their result; affected extractor results can now be retried)."
        ),
        "chrome_processes_orphaned": (
            "Stopping {count} leftover browser process(es) "
            "(ArchiveBox may have been interrupted before it was able to close them; this frees browser resources and avoids duplicate browser sessions)."
        ),
        "crawls_queued_without_retry_at": (
            "Starting {count} Crawl(s) that were queued but never started "
            "(ArchiveBox may have been interrupted before it was able to begin archiving them)."
        ),
        "snapshots_queued_without_retry_at": (
            "Starting {count} Snapshot(s) that were queued but never started "
            "(ArchiveBox may have been interrupted before it was able to archive those URLs)."
        ),
        "archiveresults_backoff": (
            "Retrying {count} extractor result(s) that were waiting to retry "
            "(ArchiveBox may have been interrupted before it was able to try them again; affected outputs will be retried)."
        ),
        "archiveresults_started_without_running_process": (
            "Retrying {count} extractor result(s) that were interrupted before finishing "
            "(ArchiveBox may have been interrupted before it was able to save their final status; partial files will be overwritten with fresh results upon retry)."
        ),
        "snapshots_started_without_running_results": (
            "Resuming {count} Snapshot(s) that were interrupted before finishing "
            "(ArchiveBox may have been interrupted before it was able to finish archiving them; missing outputs will be retried)."
        ),
        "crawls_started_with_due_snapshots": (
            "Resuming {count} Crawl(s) with pending URLs ready to archive "
            "(ArchiveBox may have been interrupted before it was able to archive the remaining URLs; pending URLs will continue)."
        ),
        "crawls_started_waiting_on_future_snapshots": (
            "Resuming {count} Crawl(s) with URLs waiting for a later retry "
            "(ArchiveBox may have been interrupted before it was able to retry delayed URLs; they will retry later)."
        ),
        "crawls_started_without_active_snapshots": (
            "Finalizing {count} Crawl(s) that finished URL processing but were not closed cleanly "
            "(ArchiveBox may have been interrupted before it was able to save the final crawl status; archived data is not changed)."
        ),
    }
    for key, message in repair_messages.items():
        if cleaned[key]:
            recovery_console.print(f"[yellow]⚠️ Repairing: {message.format(count=cleaned[key])}[/yellow]")

    return cleaned
