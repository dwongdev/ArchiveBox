#!/usr/bin/env python3

"""
archivebox snapshot <action> [args...] [--filters]

Manage Snapshot records.

Actions:
    create  - Create Snapshots from URLs or Crawl JSONL
    list    - List Snapshots as JSONL (with optional filters)
    update  - Update Snapshots from stdin JSONL
    delete  - Delete Snapshots from stdin JSONL

Examples:
    # Create
    archivebox snapshot create https://example.com --tag=news
    archivebox crawl create https://example.com | archivebox snapshot create

    # List with filters
    archivebox snapshot list --status=queued
    archivebox snapshot list --url__icontains=example.com

    # Update
    archivebox snapshot list --tag=old | archivebox snapshot update --tag=new

    # Delete
    archivebox snapshot list --url__icontains=spam.com | archivebox snapshot delete --yes
"""

__package__ = "archivebox.cli"
__command__ = "archivebox snapshot"

import sys
from collections.abc import Iterable

import rich_click as click
from rich import print as rprint
from django.db.models import Case, IntegerField, Q, QuerySet, When

from archivebox.cli.cli_utils import apply_filters


# =============================================================================
# CREATE
# =============================================================================


def create_snapshots(
    urls: Iterable[str],
    tag: str = "",
    status: str = "queued",
    depth: int = 0,
    created_by_id: int | None = None,
) -> int:
    """
    Create Snapshots from URLs or stdin JSONL (Crawl or Snapshot records).
    Pass-through: Records that are not Crawl/Snapshot/URL are output unchanged.

    Exit codes:
        0: Success
        1: Failure
    """
    from archivebox.misc.jsonl import (
        read_args_or_stdin,
        write_record,
        TYPE_SNAPSHOT,
        TYPE_CRAWL,
    )
    from archivebox.misc.util import validate_url_length
    from archivebox.base_models.models import get_or_create_system_user_pk
    from archivebox.core.models import Snapshot
    from archivebox.crawls.models import Crawl

    created_by_id = created_by_id or get_or_create_system_user_pk()
    is_tty = sys.stdout.isatty()

    # Collect all input records
    records = list(read_args_or_stdin(urls))

    if not records:
        rprint("[yellow]No URLs or Crawls provided. Pass URLs as arguments or via stdin.[/yellow]", file=sys.stderr)
        return 1

    # Process each record - handle Crawls and plain URLs/Snapshots
    created_snapshots = []
    pass_through_count = 0

    for record in records:
        record_type = record.get("type", "")

        try:
            if record_type == TYPE_CRAWL:
                # Pass through the Crawl record itself first
                if not is_tty:
                    write_record(record)

                # Input is a Crawl - get or create it, then create Snapshots for its URLs
                crawl = None
                crawl_id = record.get("id")
                if crawl_id:
                    try:
                        crawl = Crawl.objects.get(id=crawl_id)
                    except Crawl.DoesNotExist:
                        crawl = Crawl.from_json(record, overrides={"created_by_id": created_by_id})
                else:
                    crawl = Crawl.from_json(record, overrides={"created_by_id": created_by_id})

                if not crawl:
                    continue

                # Create snapshots for each URL in the crawl
                for url in crawl.get_urls_list():
                    try:
                        validate_url_length(url)
                    except ValueError as err:
                        rprint(f"[red]Error creating snapshot: {err}[/red]", file=sys.stderr)
                        continue
                    merged_tags = crawl.tags_str
                    if tag:
                        merged_tags = f"{merged_tags},{tag}" if merged_tags else tag
                    snapshot_record = {
                        "url": url,
                        "tags": merged_tags,
                        "crawl_id": str(crawl.id),
                        "depth": depth,
                        "status": status,
                    }
                    snapshot = Snapshot.from_json(snapshot_record, overrides={"created_by_id": created_by_id})
                    if snapshot:
                        created_snapshots.append(snapshot)
                        if not is_tty:
                            write_record(snapshot.to_json())

            elif record_type == TYPE_SNAPSHOT or record.get("url"):
                # Input is a Snapshot or plain URL
                if record.get("url"):
                    validate_url_length(str(record["url"]))
                if tag and not record.get("tags"):
                    record["tags"] = tag
                if status:
                    record["status"] = status
                record["depth"] = record.get("depth", depth)

                snapshot = Snapshot.from_json(record, overrides={"created_by_id": created_by_id})
                if snapshot:
                    created_snapshots.append(snapshot)
                    if not is_tty:
                        write_record(snapshot.to_json())

            else:
                # Pass-through: output records we don't handle
                if not is_tty:
                    write_record(record)
                pass_through_count += 1

        except Exception as e:
            rprint(f"[red]Error creating snapshot: {e}[/red]", file=sys.stderr)
            continue

    if not created_snapshots:
        if pass_through_count > 0:
            rprint(f"[dim]Passed through {pass_through_count} records, no new snapshots[/dim]", file=sys.stderr)
            return 0
        rprint("[red]No snapshots created[/red]", file=sys.stderr)
        return 1

    rprint(f"[green]Created {len(created_snapshots)} snapshots[/green]", file=sys.stderr)

    if is_tty:
        for snapshot in created_snapshots:
            rprint(f"  [dim]{snapshot.id}[/dim] {snapshot.url[:60]}", file=sys.stderr)

    return 0


# =============================================================================
# LIST
# =============================================================================


def build_snapshot_queryset(
    *,
    status: str | None = None,
    url__icontains: str | None = None,
    url__istartswith: str | None = None,
    tag: str | None = None,
    crawl_id: str | None = None,
    sort: str | None = None,
    search: str | None = None,
    query: str | None = None,
    limit: int | None = None,
) -> QuerySet:
    from archivebox.core.models import Snapshot
    from archivebox.search import (
        get_default_search_mode,
        get_search_mode,
        prioritize_metadata_matches,
        query_search_index,
    )

    queryset = Snapshot.objects.order_by("-created_at")
    queryset = apply_filters(
        queryset,
        {
            "status": status,
            "url__icontains": url__icontains,
            "url__istartswith": url__istartswith,
            "crawl_id": crawl_id,
        },
    )

    if tag:
        queryset = queryset.filter(tags__name__iexact=tag)

    query = (query or "").strip()
    if query:
        metadata_qs = queryset.filter(
            Q(title__icontains=query) | Q(url__icontains=query) | Q(timestamp__icontains=query) | Q(tags__name__icontains=query),
        )
        requested_search_mode = (search or "").strip().lower()
        if requested_search_mode == "content":
            requested_search_mode = "contents"
        search_mode = get_default_search_mode() if not requested_search_mode else get_search_mode(requested_search_mode)

        if search_mode == "meta":
            queryset = metadata_qs
        elif limit and len(list(metadata_qs.values_list("pk", flat=True).distinct()[:limit])) >= limit:
            queryset = metadata_qs
        else:
            try:
                deep_qsearch = None
                if search_mode == "deep":
                    qsearch = query_search_index(query, search_mode="contents", max_results=limit)
                    deep_qsearch = query_search_index(query, search_mode="deep", max_results=limit)
                else:
                    qsearch = query_search_index(query, search_mode=search_mode, max_results=limit)
                queryset = prioritize_metadata_matches(
                    queryset,
                    metadata_qs,
                    qsearch,
                    deep_queryset=deep_qsearch,
                    ordering=("-created_at",) if not sort else None,
                )
            except Exception as err:
                rprint(
                    f"[yellow]Search backend error, falling back to metadata search: {err}[/yellow]",
                    file=sys.stderr,
                )
                queryset = metadata_qs

    if sort:
        queryset = queryset.order_by(sort)

    return queryset


def list_snapshots(
    status: str | None = None,
    url__icontains: str | None = None,
    url__istartswith: str | None = None,
    tag: str | None = None,
    crawl_id: str | None = None,
    limit: int | None = None,
    sort: str | None = None,
    csv: str | None = None,
    with_headers: bool = False,
    search: str | None = None,
    query: str | None = None,
) -> int:
    """
    List Snapshots as JSONL with optional filters.

    Exit codes:
        0: Success (even if no results)
    """
    from archivebox.misc.jsonl import write_record
    from archivebox.core.models import Snapshot

    if with_headers and not csv:
        rprint("[red]--with-headers requires --csv[/red]", file=sys.stderr)
        return 2

    is_tty = sys.stdout.isatty() and not csv

    queryset = build_snapshot_queryset(
        status=status,
        url__icontains=url__icontains,
        url__istartswith=url__istartswith,
        tag=tag,
        crawl_id=crawl_id,
        sort=sort,
        search=search,
        query=query,
        limit=limit,
    )

    if not is_tty:
        if limit:
            limited_ids = list(queryset.values_list("id", flat=True)[:limit])
            preserved_order = Case(
                *(When(id=snapshot_id, then=position) for position, snapshot_id in enumerate(limited_ids)),
                output_field=IntegerField(),
            )
            queryset = Snapshot.objects.filter(id__in=limited_ids).order_by(preserved_order)
        queryset = queryset.prefetch_related("tags")
    elif limit:
        queryset = queryset[:limit]

    count = 0
    if csv:
        cols = [col.strip() for col in csv.split(",") if col.strip()]
        if not cols:
            rprint("[red]No CSV columns provided[/red]", file=sys.stderr)
            return 2
        rows: list[str] = []
        if with_headers:
            rows.append(",".join(cols))
        simple_cols = {
            "id",
            "crawl_id",
            "url",
            "title",
            "timestamp",
            "depth",
            "status",
            "fs_version",
            "bookmarked_at",
            "created_at",
            "modified_at",
            "retry_at",
            "downloaded_at",
        }
        from archivebox.misc.util import to_json

        for snapshot in queryset.iterator(chunk_size=500):
            if set(cols).issubset(simple_cols):
                rows.append(
                    ",".join(
                        to_json(
                            value.isoformat() if hasattr((value := getattr(snapshot, col, "")), "isoformat") else value,
                            indent=None,
                        )
                        for col in cols
                    ),
                )
            else:
                rows.append(snapshot.to_csv(cols=cols, separator=","))
            count += 1
        output = "\n".join(rows)
        if output:
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
        rprint(f"[dim]Listed {count} snapshots[/dim]", file=sys.stderr)
        return 0

    for snapshot in queryset:
        if is_tty:
            status_color = {
                "queued": "yellow",
                "started": "blue",
                "sealed": "green",
            }.get(snapshot.status, "dim")
            rprint(f"[{status_color}]{snapshot.status:8}[/{status_color}] [dim]{snapshot.id}[/dim] {snapshot.url[:60]}")
        else:
            write_record(snapshot.to_json())
        count += 1

    rprint(f"[dim]Listed {count} snapshots[/dim]", file=sys.stderr)
    return 0


# =============================================================================
# UPDATE
# =============================================================================


def update_snapshots(
    status: str | None = None,
    tag: str | None = None,
) -> int:
    """
    Update Snapshots from stdin JSONL.

    Reads Snapshot records from stdin and applies updates.
    Uses PATCH semantics - only specified fields are updated.

    Exit codes:
        0: Success
        1: No input or error
    """
    from django.utils import timezone

    from archivebox.misc.jsonl import read_stdin, write_record
    from archivebox.core.models import Snapshot

    is_tty = sys.stdout.isatty()

    records = list(read_stdin())
    if not records:
        rprint("[yellow]No records provided via stdin[/yellow]", file=sys.stderr)
        return 1

    updated_count = 0
    for record in records:
        snapshot_id = record.get("id")
        if not snapshot_id:
            continue

        try:
            snapshot = Snapshot.objects.get(id=snapshot_id)

            if status:
                if status not in Snapshot.StatusChoices.values:
                    rprint(f"[red]Invalid snapshot status: {status}[/red]", file=sys.stderr)
                    continue
                if status == Snapshot.StatusChoices.SEALED:
                    snapshot.cancel()
                elif status == Snapshot.StatusChoices.PAUSED:
                    snapshot.pause()
                elif status == Snapshot.StatusChoices.QUEUED:
                    if snapshot.status == Snapshot.StatusChoices.PAUSED:
                        snapshot.resume()
                    else:
                        snapshot.update_and_requeue(status=Snapshot.StatusChoices.QUEUED, retry_at=timezone.now())
                elif status == Snapshot.StatusChoices.STARTED:
                    snapshot.update_and_requeue(status=Snapshot.StatusChoices.STARTED, retry_at=timezone.now())
            if tag:
                from archivebox.core.models import Tag

                tag_obj, _ = Tag.objects.get_or_create(name=tag)
                snapshot.tags.add(tag_obj)
                snapshot.safe_update({"modified_at": timezone.now()}, refresh=False)

            if not status and not tag:
                snapshot.safe_update({"modified_at": timezone.now()}, refresh=False)
            updated_count += 1

            if not is_tty:
                snapshot.refresh_from_db()
                write_record(snapshot.to_json())

        except Snapshot.DoesNotExist:
            rprint(f"[yellow]Snapshot not found: {snapshot_id}[/yellow]", file=sys.stderr)
            continue

    rprint(f"[green]Updated {updated_count} snapshots[/green]", file=sys.stderr)
    return 0


# =============================================================================
# DELETE
# =============================================================================


def delete_snapshots(yes: bool = False, dry_run: bool = False) -> int:
    """
    Delete Snapshots from stdin JSONL.

    Requires --yes flag to confirm deletion.

    Exit codes:
        0: Success
        1: No input or missing --yes flag
    """
    from archivebox.misc.jsonl import read_stdin
    from archivebox.core.models import Snapshot

    records = list(read_stdin())
    if not records:
        rprint("[yellow]No records provided via stdin[/yellow]", file=sys.stderr)
        return 1

    snapshot_ids = [r.get("id") for r in records if r.get("id")]

    if not snapshot_ids:
        rprint("[yellow]No valid snapshot IDs in input[/yellow]", file=sys.stderr)
        return 1

    snapshots = Snapshot.objects.filter(id__in=snapshot_ids)
    count = snapshots.count()

    if count == 0:
        rprint("[yellow]No matching snapshots found[/yellow]", file=sys.stderr)
        return 0

    if dry_run:
        rprint(f"[yellow]Would delete {count} snapshots (dry run)[/yellow]", file=sys.stderr)
        for snapshot in snapshots:
            rprint(f"  [dim]{snapshot.id}[/dim] {snapshot.url[:60]}", file=sys.stderr)
        return 0

    if not yes:
        rprint("[red]Use --yes to confirm deletion[/red]", file=sys.stderr)
        return 1

    # Perform deletion
    deleted_count, _ = snapshots.delete()
    rprint(f"[green]Deleted {deleted_count} snapshots[/green]", file=sys.stderr)
    return 0


# =============================================================================
# CLI Commands
# =============================================================================


@click.group()
def main():
    """Manage Snapshot records."""
    pass


@main.command("create")
@click.argument("urls", nargs=-1)
@click.option("--tag", "-t", default="", help="Comma-separated tags to add")
@click.option("--status", "-s", default="queued", help="Initial status (default: queued)")
@click.option("--depth", "-d", type=int, default=0, help="Crawl depth (default: 0)")
def create_cmd(urls: tuple, tag: str, status: str, depth: int):
    """Create Snapshots from URLs or stdin JSONL."""
    sys.exit(create_snapshots(urls, tag=tag, status=status, depth=depth))


@main.command("list")
@click.option("--status", "-s", help="Filter by status (queued, started, sealed)")
@click.option("--url__icontains", help="Filter by URL contains")
@click.option("--url__istartswith", help="Filter by URL starts with")
@click.option("--tag", "-t", help="Filter by tag name")
@click.option("--crawl-id", help="Filter by crawl ID")
@click.option("--limit", "-n", type=int, help="Limit number of results")
@click.option("--sort", "-o", type=str, help="Field to sort by, e.g. url, created_at, bookmarked_at, downloaded_at")
@click.option("--csv", "-C", type=str, help="Print output as CSV with the provided fields, e.g.: timestamp,url,title")
@click.option("--with-headers", is_flag=True, help="Include column headers in structured output")
@click.option("--search", type=click.Choice(["meta", "content", "contents", "deep"]), help="Search mode to use for the query")
@click.argument("query", nargs=-1)
def list_cmd(
    status: str | None,
    url__icontains: str | None,
    url__istartswith: str | None,
    tag: str | None,
    crawl_id: str | None,
    limit: int | None,
    sort: str | None,
    csv: str | None,
    with_headers: bool,
    search: str | None,
    query: tuple[str, ...],
):
    """List Snapshots as JSONL."""
    sys.exit(
        list_snapshots(
            status=status,
            url__icontains=url__icontains,
            url__istartswith=url__istartswith,
            tag=tag,
            crawl_id=crawl_id,
            limit=limit,
            sort=sort,
            csv=csv,
            with_headers=with_headers,
            search=search,
            query=" ".join(query),
        ),
    )


@main.command("update")
@click.option("--status", "-s", help="Set status")
@click.option("--tag", "-t", help="Add tag")
def update_cmd(status: str | None, tag: str | None):
    """Update Snapshots from stdin JSONL."""
    sys.exit(update_snapshots(status=status, tag=tag))


@main.command("delete")
@click.option("--yes", "-y", is_flag=True, help="Confirm deletion")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted")
def delete_cmd(yes: bool, dry_run: bool):
    """Delete Snapshots from stdin JSONL."""
    sys.exit(delete_snapshots(yes=yes, dry_run=dry_run))


if __name__ == "__main__":
    main()
