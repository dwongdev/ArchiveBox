__package__ = "archivebox.search"

import asyncio
import hashlib
import json
import threading
from copy import copy
from queue import Full, Queue
from urllib.parse import urlsplit
from uuid import UUID

from django.core.cache import cache
from django.db.models import Q
from django.http import QueryDict, StreamingHttpResponse

from archivebox.search.config import get_search_mode, get_search_mode_base
from archivebox.search.query import crawl_config_values_search_wave, iter_query_search_ids


SEARCH_RESULT_CACHE_TTL = 60


def get_admin_search_cache_key(request, url: str | None = None) -> str:
    """Build the cache key for one user and changelist URL."""
    # Search streams publish IDs for one exact changelist URL. Keeping the URL
    # whole makes sidebar filters, ordering, and user scope part of the key.
    payload = json.dumps(
        {
            "user": str(request.user.pk or "anon"),
            "url": url or request.get_full_path(),
        },
        sort_keys=True,
    )
    return f"abx:admin-search:{hashlib.sha256(payload.encode()).hexdigest()}"


def get_cached_admin_search_ids(request) -> list[str] | None:
    """Return streamed admin search IDs from Django cache."""
    cached = cache.get(get_admin_search_cache_key(request))
    if isinstance(cached, dict):
        return cached.get("ids") or []
    return None


def iter_admin_meta_search_ids(query, queryset):
    """Yield metadata search matches from a filtered Snapshot queryset."""
    seen = set()
    try:
        snapshot_id = UUID(query)
    except ValueError:
        snapshot_id = None
    if snapshot_id:
        for pk in queryset.filter(pk=snapshot_id).values_list("pk", flat=True):
            seen.add(pk)
            yield pk

    waves = [
        Q(timestamp__startswith=query) | Q(url__istartswith=query) | Q(title__istartswith=query),
        Q(url__icontains=query),
        Q(title__icontains=query),
        Q(tags__name__icontains=query),
        Q(notes__icontains=query) | Q(crawl__notes__icontains=query) | Q(crawl__label__icontains=query),
        Q(crawl__created_by__username=query),
    ]
    config_wave = crawl_config_values_search_wave(query)
    if config_wave is not None:
        waves.append(config_wave)

    for wave in waves:
        for pk in queryset.filter(wave).values_list("pk", flat=True).distinct().iterator(chunk_size=500):
            if pk in seen:
                continue
            seen.add(pk)
            yield pk


def iter_admin_backend_search_ids(iterator, queryset):
    """Yield backend search IDs that still match the filtered queryset."""
    batch = []
    seen = set()

    def flush_batch():
        valid = {str(pk) for pk in queryset.filter(pk__in=batch).values_list("pk", flat=True)}
        for snapshot_id in batch:
            if snapshot_id in valid and snapshot_id not in seen:
                seen.add(snapshot_id)
                yield snapshot_id

    for snapshot_id in iterator:
        snapshot_id = str(snapshot_id).strip().lower().replace("-", "")
        if len(snapshot_id) != 32:
            continue
        batch.append(snapshot_id)
        if len(batch) >= 200:
            yield from flush_batch()
            batch = []
    if batch:
        yield from flush_batch()


def admin_snapshot_search_stream_view(model_admin, request):
    """Stream admin Snapshot search progress and cache matching IDs."""
    query = (request.GET.get("q") or "").strip()
    config = request.archivebox_config
    search_mode = get_search_mode(request.GET.get("search_mode"), config=config)
    if not query:
        return StreamingHttpResponse((), content_type="text/plain")

    search_url = request.GET.get("search_url") or request.get_full_path()
    target_url = urlsplit(search_url)
    target_get = QueryDict(target_url.query, mutable=True)
    for key in ("q", "search_mode", "p", "search_url"):
        target_get.pop(key, None)

    filter_request = copy(request)
    filter_request.path = target_url.path or request.path
    filter_request.path_info = target_url.path or request.path_info
    filter_request.GET = target_get
    filter_request.archivebox_config = config

    # Build the same filtered base queryset the changelist uses, but with the
    # search params stripped. The stream intersects each wave with this queryset
    # before writing IDs into the short-lived cache consumed by the changelist.
    current_request = model_admin.__dict__.get("request")
    try:
        base_queryset = model_admin.get_changelist_instance(filter_request).queryset
    finally:
        model_admin.request = current_request

    async def snapshot_ids():
        seen = set()
        ids = []
        last_sent = 0
        stream_batch_size = 100
        stream_padding = " " * 4096
        cache_key = get_admin_search_cache_key(request, search_url)
        cache.set(cache_key, {"ids": ids, "done": False}, SEARCH_RESULT_CACHE_TTL)
        yield f"0{stream_padding}\n"
        queue = Queue(maxsize=8)
        stop_event = threading.Event()

        def emit(item):
            while not stop_event.is_set():
                try:
                    queue.put(item, timeout=0.1)
                    return
                except Full:
                    continue

        def run_search():
            nonlocal last_sent
            iterator = None
            try:
                search_mode_base = get_search_mode_base(search_mode, config=config)
                iterator = (
                    iter_admin_meta_search_ids(query, base_queryset)
                    if search_mode_base == "meta"
                    else iter_admin_backend_search_ids(
                        iter_query_search_ids(query, search_mode=search_mode, config=config),
                        base_queryset,
                    )
                )
                for snapshot_id in iterator:
                    if stop_event.is_set():
                        break
                    snapshot_id = str(snapshot_id).strip().lower().replace("-", "")
                    if len(snapshot_id) != 32 or snapshot_id in seen:
                        continue
                    seen.add(snapshot_id)
                    ids.append(snapshot_id)
                    if len(ids) - last_sent >= stream_batch_size:
                        cache.set(cache_key, {"ids": ids, "done": False}, SEARCH_RESULT_CACHE_TTL)
                        last_sent = len(ids)
                        emit(f"{last_sent}{stream_padding}\n")
                if not stop_event.is_set() and len(ids) != last_sent:
                    cache.set(cache_key, {"ids": ids, "done": False}, SEARCH_RESULT_CACHE_TTL)
                    emit(f"{len(ids)}{stream_padding}\n")
            except BaseException as err:
                emit(err)
            finally:
                if iterator is not None:
                    try:
                        iterator.close()
                    except AttributeError:
                        pass
                cache.set(cache_key, {"ids": ids, "done": True}, SEARCH_RESULT_CACHE_TTL)
                emit(None)

        threading.Thread(target=run_search, name="admin-snapshot-search-stream", daemon=True).start()
        try:
            while True:
                item = await asyncio.to_thread(queue.get)
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop_event.set()

    response = StreamingHttpResponse(snapshot_ids(), content_type="text/plain")
    response["X-Accel-Buffering"] = "no"
    return response
