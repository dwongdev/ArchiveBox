import os
import re
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from datetime import timedelta
from urllib.parse import urlencode

import pytest
import requests
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from archivebox.misc.logging import AttrDict
from archivebox.tests.conftest import (
    cli_env,
    create_admin_and_token,
    get_free_port,
    run_archivebox_cmd,
    start_archivebox_server,
    stop_archivebox_process,
    wait_for_http,
)


pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
ADMIN_HOST = "admin.archivebox.localhost:8000"
PUBLIC_HOST = "public.archivebox.localhost:8000"


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        username="testadmin",
        email="admin@test.com",
        password="testpassword",
    )


@pytest.fixture
def crawl(admin_user, db):
    from archivebox.crawls.models import Crawl

    return Crawl.objects.create(
        urls="https://example.com",
        created_by=admin_user,
    )


@pytest.fixture
def snapshot(crawl, db):
    from archivebox.core.models import Snapshot

    return Snapshot.objects.create(
        url="https://example.com",
        crawl=crawl,
        status=Snapshot.StatusChoices.STARTED,
    )


@pytest.fixture
def public_snapshot(crawl, db):
    from archivebox.core.models import Snapshot

    return Snapshot.objects.create(
        url="https://public-example.com",
        title="Public Example Website",
        crawl=crawl,
        status=Snapshot.StatusChoices.SEALED,
    )


def consume_streaming_response(response):
    if response.is_async:

        async def consume():
            return b"".join([chunk async for chunk in response.streaming_content])

        return async_to_sync(consume)()
    return b"".join(response.streaming_content)


def collect_streaming_response_chunks(response):
    if response.is_async:

        async def consume():
            return [chunk async for chunk in response.streaming_content]

        return async_to_sync(consume)()
    return list(response.streaming_content)


def populate_admin_search_cache(client, path, params):
    search_url = f"{path}?{urlencode(params)}"
    response = client.get(
        reverse("admin:core_snapshot_search_stream"),
        {**params, "search_url": search_url},
        HTTP_HOST=ADMIN_HOST,
    )
    assert response.status_code == 200
    assert consume_streaming_response(response)
    return client.get(path, params, HTTP_HOST=ADMIN_HOST)


def test_search_backend_env_exposes_resolved_runtime_config(tmp_path):
    from archivebox.search.backends import search_backend_env

    old_env = os.environ.get("SEARCH_BACKEND_SONIC_HOST_NAME")
    os.environ["SEARCH_BACKEND_SONIC_HOST_NAME"] = "old-host"
    config = AttrDict(
        {
            "SEARCH_BACKEND_ENGINE": "sonic",
            "SEARCH_BACKEND_SONIC_HOST_NAME": "sonic",
            "SEARCH_BACKEND_SONIC_PORT": 1491,
            "SEARCH_BACKEND_SONIC_PASSWORD": "SecretPassword",
            "IGNORED_NONE_VALUE": None,
        },
    )

    try:
        with search_backend_env(config=config):
            assert os.environ["SEARCH_BACKEND_ENGINE"] == "sonic"
            assert os.environ["SEARCH_BACKEND_SONIC_HOST_NAME"] == "sonic"
            assert os.environ["SEARCH_BACKEND_SONIC_PORT"] == "1491"
            assert os.environ["SEARCH_BACKEND_SONIC_PASSWORD"] == "SecretPassword"
            assert "IGNORED_NONE_VALUE" not in os.environ

        assert os.environ["SEARCH_BACKEND_SONIC_HOST_NAME"] == "old-host"
    finally:
        if old_env is None:
            os.environ.pop("SEARCH_BACKEND_SONIC_HOST_NAME", None)
        else:
            os.environ["SEARCH_BACKEND_SONIC_HOST_NAME"] = old_env


def test_search_mode_options_use_canonical_backend_names(monkeypatch):
    from archivebox.search.config import get_search_mode_options

    monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")

    options = get_search_mode_options()

    assert {"value": "contents", "label": "deep"} in options
    assert {"value": "deep:ripgrep", "label": "deep:ripgrep"} in options
    assert all(not option["label"].startswith("deep: ") for option in options)


def test_snapshot_metadata_search_includes_notes_crawl_fields_username_and_config_values(admin_user):
    from archivebox.core.models import Snapshot
    from archivebox.crawls.models import Crawl
    from archivebox.search.query import apply_snapshot_search

    crawl = Crawl.objects.create(
        urls="https://example.com",
        created_by=admin_user,
        label="crawl-label-needle",
        notes="crawl-notes-needle",
        config={
            "KEY_ONLY_NEEDLE": "unrelated-value",
            "SIMPLE_VALUE": "crawl-config-value-needle",
            "NESTED": {"INNER": "nested-config-value-needle"},
        },
    )
    snapshot = Snapshot.objects.create(
        url="https://example.com/metadata-extra",
        title="Unrelated title",
        notes="snapshot-notes-needle",
        crawl=crawl,
    )

    def search_ids(query: str):
        return set(apply_snapshot_search(Snapshot.objects.all(), query, search_mode="meta").values_list("pk", flat=True))

    assert snapshot.pk in search_ids("snapshot-notes-needle")
    assert snapshot.pk in search_ids("crawl-notes-needle")
    assert snapshot.pk in search_ids("crawl-label-needle")
    assert snapshot.pk in search_ids("testadmin")
    assert snapshot.pk not in search_ids("testad")
    assert snapshot.pk not in search_ids("KEY_ONLY_NEEDLE")


class TestAdminSnapshotSearch:
    def test_admin_search_mode_selector_defaults_to_configured_deep_backend_for_ripgrep(self, client, admin_user, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")

        client.login(username="testadmin", password="testpassword")
        response = client.get(reverse("admin:core_snapshot_changelist"), HTTP_HOST=ADMIN_HOST)

        assert response.status_code == 200
        assert response.context["cl"].search_mode == "deep:ripgrep"
        assert b'name="search_mode"' in response.content
        assert b'<option value="contents">deep</option>' in response.content
        assert b">contents<" not in response.content
        assert b'value="deep:ripgrep"' in response.content
        assert b">deep:ripgrep<" in response.content

    def test_admin_search_mode_selector_defaults_to_configured_deep_backend_for_sqlite(self, client, admin_user, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "sqlite")

        client.login(username="testadmin", password="testpassword")
        response = client.get(reverse("admin:core_snapshot_changelist"), HTTP_HOST=ADMIN_HOST)

        assert response.status_code == 200
        assert response.context["cl"].search_mode == "deep:sqlite"

    def test_admin_search_mode_selector_stays_checked_after_search(self, client, admin_user, crawl):
        from archivebox.core.models import Snapshot

        Snapshot.objects.create(
            url="https://example.com/fulltext-only",
            title="Unrelated Title",
            crawl=crawl,
        )

        client.login(username="testadmin", password="testpassword")
        response = client.get(
            reverse("admin:core_snapshot_changelist"),
            {"q": "google", "search_mode": "contents"},
            HTTP_HOST=ADMIN_HOST,
        )

        assert response.status_code == 200
        assert response.context["cl"].search_mode == "contents"
        assert b'id="changelist"' in response.content
        assert b"search-mode-contents" in response.content

    def test_admin_search_stream_uses_real_ripgrep_backend_for_deep_results(self, client, admin_user, crawl, monkeypatch):
        from archivebox.core.models import Snapshot

        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")
        fulltext_snapshot = Snapshot.objects.create(
            url="https://example.com/fulltext-only",
            title="Unrelated Title",
            crawl=crawl,
        )
        output_file = fulltext_snapshot.output_dir / "dom" / "output.html"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("<html><body>needle-deep-result</body></html>", encoding="utf-8")

        client.login(username="testadmin", password="testpassword")
        response = populate_admin_search_cache(
            client,
            reverse("admin:core_snapshot_changelist"),
            {"q": "needle-deep-result", "search_mode": "deep"},
        )

        assert response.status_code == 200
        assert response.context["cl"].search_mode.startswith("deep")
        assert b"search-mode-deep" in response.content
        assert str(fulltext_snapshot.id).encode() in response.content

    def test_admin_meta_search_streams_results_in_metadata_wave_order(self, client, admin_user, crawl):
        from archivebox.core.models import Snapshot

        prefix_snapshot = Snapshot.objects.create(
            url="https://google.example.com/prefix",
            title="Later Title",
            timestamp="2000000000",
            crawl=crawl,
        )
        contains_snapshot = Snapshot.objects.create(
            url="https://example.com/path/google-contained",
            title="Later Title",
            timestamp="3000000000",
            crawl=crawl,
        )
        title_snapshot = Snapshot.objects.create(
            url="https://example.com/title-only",
            title="Google Title Match",
            timestamp="1000000000",
            crawl=crawl,
        )

        client.login(username="testadmin", password="testpassword")
        path = reverse("admin:core_snapshot_changelist")
        params = {"q": "google", "search_mode": "meta"}
        response = populate_admin_search_cache(client, path, params)

        assert response.status_code == 200
        from archivebox.search.views import get_admin_search_cache_key

        cached = cache.get(get_admin_search_cache_key(SimpleNamespace(user=admin_user), f"{path}?{urlencode(params)}"))
        assert cached["ids"][:3] == [str(prefix_snapshot.pk), str(title_snapshot.pk), str(contains_snapshot.pk)]
        result_ids = list(response.context["cl"].queryset.values_list("pk", flat=True))
        assert {title_snapshot.pk, contains_snapshot.pk, prefix_snapshot.pk}.issubset(result_ids)

    def test_admin_contents_search_stream_uses_real_backend_results(self, client, admin_user, crawl, monkeypatch):
        from archivebox.core.models import Snapshot

        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")
        metadata_snapshot = Snapshot.objects.create(
            url="https://example.com/google-meta",
            title="Google Metadata Match",
            crawl=crawl,
        )
        fulltext_snapshot = Snapshot.objects.create(
            url="https://example.com/fulltext-only",
            title="Unrelated Title",
            crawl=crawl,
        )
        output_file = fulltext_snapshot.output_dir / "dom" / "output.html"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("<html><body>google fulltext match</body></html>", encoding="utf-8")

        client.login(username="testadmin", password="testpassword")
        response = populate_admin_search_cache(
            client,
            reverse("admin:core_snapshot_changelist"),
            {"q": "google", "search_mode": "contents"},
        )

        assert response.status_code == 200
        result_ids = list(response.context["cl"].queryset.values_list("pk", flat=True))
        assert metadata_snapshot.pk not in result_ids
        assert result_ids[:1] == [fulltext_snapshot.pk]

    def test_manual_admin_sort_applies_to_cached_search_results(self, client, admin_user, crawl):
        from archivebox.core.models import Snapshot

        older_snapshot = Snapshot.objects.create(
            url="https://example.com/google-older",
            title="A Google Older",
            timestamp="1000000000",
            crawl=crawl,
        )
        newer_snapshot = Snapshot.objects.create(
            url="https://example.com/google-newer",
            title="Z Google Newer",
            timestamp="2000000000",
            crawl=crawl,
        )

        client.login(username="testadmin", password="testpassword")
        response = populate_admin_search_cache(
            client,
            reverse("admin:core_snapshot_changelist"),
            {"q": "google", "search_mode": "meta", "o": "4"},
        )

        assert response.status_code == 200
        result_ids = list(response.context["cl"].queryset.values_list("pk", flat=True))
        assert result_ids[:2] == [older_snapshot.pk, newer_snapshot.pk]

    def test_search_by_url(self, client, admin_user, snapshot):
        client.login(username="testadmin", password="testpassword")
        response = client.get(reverse("admin:core_snapshot_changelist"), {"q": "example.com"}, HTTP_HOST=ADMIN_HOST)

        assert response.status_code == 200
        assert b"example.com" in response.content

    def test_search_by_title(self, client, admin_user, crawl, db):
        from archivebox.core.models import Snapshot

        Snapshot.objects.create(
            url="https://example.com/titled",
            title="Unique Title For Testing",
            crawl=crawl,
        )

        client.login(username="testadmin", password="testpassword")
        response = client.get(reverse("admin:core_snapshot_changelist"), {"q": "Unique Title"}, HTTP_HOST=ADMIN_HOST)

        assert response.status_code == 200

    def test_search_by_tag(self, client, admin_user, snapshot, db):
        from archivebox.core.models import Tag

        tag = Tag.objects.create(name="test-search-tag")
        snapshot.tags.add(tag)

        client.login(username="testadmin", password="testpassword")
        response = client.get(reverse("admin:core_snapshot_changelist"), {"q": "test-search-tag"}, HTTP_HOST=ADMIN_HOST)

        assert response.status_code == 200

    def test_empty_search(self, client, admin_user):
        client.login(username="testadmin", password="testpassword")
        response = client.get(reverse("admin:core_snapshot_changelist"), {"q": ""}, HTTP_HOST=ADMIN_HOST)

        assert response.status_code == 200

    def test_no_results_search(self, client, admin_user):
        client.login(username="testadmin", password="testpassword")
        response = client.get(reverse("admin:core_snapshot_changelist"), {"q": "nonexistent-url-xyz789"}, HTTP_HOST=ADMIN_HOST)

        assert response.status_code == 200


class TestPublicIndexSearch:
    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_by_url(self, client, public_snapshot):
        cache.clear()
        response = client.get("/public/", {"q": "public-example.com"}, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        assert b"matching snapshots..." in response.content
        assert b"No snapshots found." not in response.content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_mode_selector_defaults_to_configured_deep_backend_for_ripgrep(self, client, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")

        response = client.get("/public/", HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        assert response.context["search_mode"] == "deep:ripgrep"
        assert b'name="search_mode"' in response.content
        assert b'<option value="contents">deep</option>' in response.content
        assert b">contents<" not in response.content
        assert b'value="deep:ripgrep"' in response.content
        assert b">deep:ripgrep<" in response.content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_uses_streamed_metadata_order(self, client, crawl, monkeypatch):
        from archivebox.core.models import Snapshot

        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")
        metadata_snapshot = Snapshot.objects.create(
            url="https://public-example.com/google-meta",
            title="Google Metadata Match",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        fulltext_snapshot = Snapshot.objects.create(
            url="https://public-example.com/google-url-only",
            title="Unrelated Title",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        output_file = fulltext_snapshot.output_dir / "dom" / "output.html"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("<html><body>google public fulltext match</body></html>", encoding="utf-8")

        search_params = {"q": "google", "search_mode": "meta"}
        search_url = f"/public/?{urlencode(search_params)}"
        stream_response = client.get(
            "/public/search-stream/",
            {**search_params, "search_url": search_url},
            HTTP_HOST=PUBLIC_HOST,
        )
        assert stream_response.status_code == 200
        assert consume_streaming_response(stream_response)

        response = client.get("/public/", search_params, HTTP_HOST=PUBLIC_HOST)

        content = response.content.decode()
        assert content.index(str(metadata_snapshot.url)) < content.index(str(fulltext_snapshot.url))

    @override_settings(PUBLIC_INDEX=True)
    def test_public_metadata_search_prioritizes_common_url_prefixes(self, crawl):
        from archivebox.core.models import Snapshot
        from archivebox.search.views import iter_admin_meta_search_ids

        broad_match = Snapshot.objects.create(
            url="https://late.example.com/path/to/iana",
            title="Unrelated Broad Match",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        prefix_match = Snapshot.objects.create(
            url="https://www.iana.org/domains/reserved",
            title="Unrelated Prefix Match",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
        )

        ids = list(iter_admin_meta_search_ids("iana", Snapshot.objects.order_by("created_at")))

        assert str(ids[0]) == str(prefix_match.pk)
        assert str(broad_match.pk) in {str(snapshot_id) for snapshot_id in ids}

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_stream_flushes_first_result_and_small_batches(self, client, crawl):
        from archivebox.core.models import Snapshot

        Snapshot.objects.bulk_create(
            [
                Snapshot(
                    url=f"https://www.fast-stream-{index}.example.com",
                    title=f"Fast Stream {index}",
                    crawl=crawl,
                    status=Snapshot.StatusChoices.SEALED,
                    timestamp=str(2_000_000_000 + index),
                )
                for index in range(6)
            ],
        )
        search_params = {"q": "fast-stream", "search_mode": "meta"}
        search_url = f"/public/?{urlencode(search_params)}"

        response = client.get(
            "/public/search-stream/",
            {**search_params, "search_url": search_url},
            HTTP_HOST=PUBLIC_HOST,
        )

        assert response.status_code == 200
        chunks = collect_streaming_response_chunks(response)
        counts = [int(chunk.strip() or b"0") for chunk in chunks if chunk.strip()]
        assert counts[:4] == [0, 1, 4, 6]

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_by_title(self, client, public_snapshot):
        response = client.get("/public/", {"q": "Public Example"}, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        assert b"archivebox-search-stream-status" in response.content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_stream_populates_public_results_cache(self, client, public_snapshot):
        search_params = {"q": "Public Example", "search_mode": "meta"}
        search_url = f"/public/?{urlencode(search_params)}"

        response = client.get(
            "/public/search-stream/",
            {**search_params, "search_url": search_url},
            HTTP_HOST=PUBLIC_HOST,
        )

        assert response.status_code == 200
        assert response["X-Accel-Buffering"] == "no"
        assert consume_streaming_response(response)

        response = client.get("/public/", search_params, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        assert b"Public Example Website" in response.content
        assert b"No snapshots found." not in response.content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_shows_exact_total_count_and_page_count_for_100_plus_snapshots(self, client, crawl, public_snapshot):
        from archivebox.core.models import Snapshot

        base_time = public_snapshot.bookmarked_at + timedelta(seconds=1)

        Snapshot.objects.bulk_create(
            [
                Snapshot(
                    url=f"https://public-page-test-{index:03d}.example.com",
                    title=f"Public Page Test {index:03d}",
                    crawl=crawl,
                    status=Snapshot.StatusChoices.SEALED,
                    config={"PERMISSIONS": "public"},
                    created_at=base_time + timedelta(seconds=index),
                    bookmarked_at=base_time + timedelta(seconds=index),
                    timestamp=str((base_time + timedelta(seconds=index)).timestamp()),
                )
                for index in range(124)
            ]
            + [
                Snapshot(
                    url=f"https://private-page-test-{index:03d}.example.com",
                    title=f"Private Page Test {index:03d}",
                    crawl=crawl,
                    status=Snapshot.StatusChoices.SEALED,
                    config={"PERMISSIONS": "private"},
                    created_at=base_time + timedelta(seconds=124 + index),
                    bookmarked_at=base_time + timedelta(seconds=124 + index),
                    timestamp=str((base_time + timedelta(seconds=124 + index)).timestamp()),
                )
                for index in range(5)
            ],
        )

        response = client.get("/public/", HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        assert response.context["paginator"].count == 125
        assert response.context["paginator"].num_pages == 3
        assert response.context["page_obj"].has_next() is True
        assert len(response.context["object_list"]) == 50
        content = response.content.decode()
        assert "1-50 of 125" in content
        assert "page 1 of 3" in content
        assert "Snapshot (125)" in content
        assert "last &raquo;" in content
        assert "private-page-test" not in content

        last_response = client.get("/public/", {"page": 3}, HTTP_HOST=PUBLIC_HOST)

        assert last_response.status_code == 200
        assert last_response.context["paginator"].count == 125
        assert last_response.context["paginator"].num_pages == 3
        assert last_response.context["page_obj"].has_next() is False
        assert len(last_response.context["object_list"]) == 25
        last_content = last_response.content.decode()
        assert "101-125 of 125" in last_content
        assert "Page 3 of 3" in last_content
        assert "last &raquo;" not in last_content
        assert "private-page-test" not in last_content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_preview_falls_back_to_extension_screenshots(self, client, public_snapshot):
        from archivebox.core.models import ArchiveResult

        ArchiveResult.objects.create(
            snapshot=public_snapshot,
            plugin="chrome_extension_screenshot",
            status=ArchiveResult.StatusChoices.SUCCEEDED,
            output_files={
                "screenshot-2.png": {"size": 2},
                "screenshot.png": {"size": 1},
                "screenshot-1.png": {"size": 3},
            },
        )

        response = client.get("/public/", HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        content = response.content.decode()
        assert "screenshot/screenshot.png" in content
        first = content.index("chrome_extension_screenshot/screenshot-1.png")
        second = content.index("chrome_extension_screenshot/screenshot.png")
        assert first < second
        assert "chrome_extension_screenshot/screenshot-2.png" not in content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_pending_snapshot_uses_small_preview_spinner(self, client, crawl):
        from archivebox.core.models import Snapshot

        Snapshot.objects.create(
            url="https://pending-public-example.com",
            title="Pending Public Example",
            crawl=crawl,
            status=Snapshot.StatusChoices.STARTED,
        )

        response = client.get("/public/", HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        content = response.content.decode()
        assert "snapshot-preview-spinner" in content
        assert "spinner.gif" in content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_finished_snapshot_without_title_falls_back_to_url(self, client, public_snapshot):
        public_snapshot.title = ""
        public_snapshot.save(update_fields=["title"])

        response = client.get("/public/", HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        content = response.content.decode()
        assert "https://public-example.com" in content
        assert "Loading..." not in content

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_query_type_meta(self, client, public_snapshot):
        response = client.get("/public/", {"q": "example", "query_type": "meta"}, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_query_type_url(self, client, public_snapshot):
        response = client.get("/public/", {"q": "public-example.com", "query_type": "url"}, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_query_type_title(self, client, public_snapshot):
        response = client.get("/public/", {"q": "Website", "query_type": "title"}, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200


class TestSearchBackendsE2E:
    @pytest.mark.timeout(360)
    def test_live_public_and_admin_search_matrix_uses_real_cli_indexing_and_streaming(self, initialized_archive):
        assert shutil.which("rg"), "ripgrep is required for the live search matrix"
        assert shutil.which("sonic"), "sonic is required for the live search matrix"

        page_count = 93
        total_snapshot_count = 100
        url_only_path = "/url-only-meta-needle.html"
        title_only_path = "/title-only.html"
        tag_only_path = "/tag-only.html"
        title_prefix_order_path = "/title-prefix-order.html"
        url_contains_order_path = "/url-orderneedle.html"
        title_contains_order_path = "/title-contains-order.html"
        tag_order_path = "/tag-order.html"
        title_only_needle = "Live Matrix Title Needle"
        tag_only_needle = "live-matrix-tag-needle"
        order_needle = "orderneedle"
        title_prefix_order_title = "Orderneedle Title Prefix Page"
        title_contains_order_title = "Contains Orderneedle Later Page"
        pages = {
            f"/page-{index:03d}.html": (
                "<!doctype html><html><head>"
                f"<title>Search Matrix Page {index:03d}</title>"
                "</head><body>"
                f"archivebox-ui-stream-needle page-{index:03d} "
                "real wget output for public admin search matrix"
                "</body></html>"
            ).encode()
            for index in range(page_count)
        }
        pages.update(
            {
                url_only_path: (
                    b"<!doctype html><html><head><title>URL Only Precision Page</title></head>"
                    b"<body>body text intentionally avoids the other precision needles</body></html>"
                ),
                title_only_path: (
                    f"<!doctype html><html><head><title>{title_only_needle}</title></head>"
                    "<body>body text intentionally avoids the title precision needle</body></html>"
                ).encode(),
                tag_only_path: (
                    b"<!doctype html><html><head><title>Tag Only Precision Page</title></head>"
                    b"<body>body text intentionally avoids the tag precision needle</body></html>"
                ),
                title_prefix_order_path: (
                    f"<!doctype html><html><head><title>{title_prefix_order_title}</title></head>"
                    "<body>body text intentionally avoids the ordering precision needle</body></html>"
                ).encode(),
                url_contains_order_path: (
                    b"<!doctype html><html><head><title>URL Contains Ordering Page</title></head>"
                    b"<body>body text intentionally avoids the ordering precision needle</body></html>"
                ),
                title_contains_order_path: (
                    f"<!doctype html><html><head><title>{title_contains_order_title}</title></head>"
                    "<body>body text intentionally avoids the ordering precision needle</body></html>"
                ).encode(),
                tag_order_path: (
                    b"<!doctype html><html><head><title>Tag Ordering Page</title></head>"
                    b"<body>body text intentionally avoids the ordering precision needle</body></html>"
                ),
            },
        )

        class SearchMatrixHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                return

        fixture_server = ThreadingHTTPServer(("127.0.0.1", 0), SearchMatrixHandler)
        fixture_thread = Thread(target=fixture_server.serve_forever, daemon=True)
        fixture_thread.start()

        archivebox_server = None
        try:
            fixture_port = fixture_server.server_address[1]
            matrix_urls = [f"http://127.0.0.1:{fixture_port}/page-{index:03d}.html" for index in range(page_count)]
            url_only_url = f"http://127.0.0.1:{fixture_port}{url_only_path}"
            title_only_url = f"http://127.0.0.1:{fixture_port}{title_only_path}"
            tag_only_url = f"http://127.0.0.1:{fixture_port}{tag_only_path}"
            title_prefix_order_url = f"http://127.0.0.1:{fixture_port}{title_prefix_order_path}"
            url_contains_order_url = f"http://127.0.0.1:{fixture_port}{url_contains_order_path}"
            title_contains_order_url = f"http://127.0.0.1:{fixture_port}{title_contains_order_path}"
            tag_order_url = f"http://127.0.0.1:{fixture_port}{tag_order_path}"
            urls = [
                *matrix_urls,
                url_only_url,
                title_only_url,
                tag_only_url,
                title_prefix_order_url,
                url_contains_order_url,
                title_contains_order_url,
                tag_order_url,
            ]
            archivebox_port = get_free_port()
            sonic_port = get_free_port()
            env = cli_env(
                live=True,
                PLUGINS="title,wget,search_backend_ripgrep,search_backend_sqlite,search_backend_sonic",
                SAVE_TITLE="True",
                SAVE_WGET="True",
                SAVE_WARC="False",
                WGET_WARC_ENABLED="False",
                SAVE_WGET_REQUISITES="False",
                WGET_TIMEOUT="20",
                TIMEOUT="20",
                PUBLIC_INDEX="True",
                PUBLIC_ADD_VIEW="True",
                PERMISSIONS="public",
                URL_ALLOWLIST=rf"127\.0\.0\.1:{fixture_port}/.*",
                URL_DENYLIST="",
                ALLOWED_HOSTS="*",
                BASE_URL=f"http://archivebox.localhost:{archivebox_port}",
                SEARCH_BACKEND_ENGINE="sonic",
                SEARCH_BACKEND_RIPGREP_ENABLED="True",
                SEARCH_BACKEND_SQLITE_ENABLED="True",
                SEARCH_BACKEND_SONIC_ENABLED="True",
                SEARCH_BACKEND_SONIC_PORT=str(sonic_port),
            )
            create_admin_and_token(initialized_archive)

            bulk_wget_urls = [*matrix_urls, url_only_url, url_contains_order_url]
            add_result = run_archivebox_cmd(
                [
                    "add",
                    "--depth=0",
                    f"--max-urls={len(bulk_wget_urls)}",
                    "--crawl-max-concurrent-snapshots=4",
                    "--parser=url_list",
                    "--plugins=wget",
                    "--tag=search-matrix",
                    *bulk_wget_urls,
                ],
                cwd=initialized_archive,
                env=env,
                timeout=180,
            )
            assert add_result.returncode == 0, add_result.stderr or add_result.stdout

            title_add_result = run_archivebox_cmd(
                [
                    "add",
                    "--depth=0",
                    "--crawl-max-concurrent-snapshots=3",
                    "--parser=url_list",
                    "--plugins=title,wget",
                    "--tag=search-matrix",
                    title_only_url,
                    title_prefix_order_url,
                    title_contains_order_url,
                ],
                cwd=initialized_archive,
                env=env,
                timeout=120,
            )
            assert title_add_result.returncode == 0, title_add_result.stderr or title_add_result.stdout

            precision_adds = (
                (tag_only_url, f"search-matrix,{tag_only_needle}", "wget"),
                (tag_order_url, f"search-matrix,{order_needle}", "wget"),
            )
            for precision_url, precision_tags, precision_plugins in precision_adds:
                precision_add_result = run_archivebox_cmd(
                    [
                        "add",
                        "--depth=0",
                        "--parser=url_list",
                        f"--plugins={precision_plugins}",
                        f"--tag={precision_tags}",
                        precision_url,
                    ],
                    cwd=initialized_archive,
                    env=env,
                    timeout=60,
                )
                assert precision_add_result.returncode == 0, precision_add_result.stderr or precision_add_result.stdout

            list_result = run_archivebox_cmd(
                ["list", "--url__icontains", f"127.0.0.1:{fixture_port}", "--csv=url"],
                cwd=initialized_archive,
                env=env,
                timeout=60,
            )
            assert list_result.returncode == 0, list_result.stderr or list_result.stdout
            listed_urls = [line.strip().strip('"') for line in list_result.stdout.splitlines() if line.strip()]
            assert len(urls) == total_snapshot_count
            assert set(listed_urls) == set(urls)

            index_update = run_archivebox_cmd(
                ["update", "--index-only", "--batch-size=10"],
                cwd=initialized_archive,
                env=env,
                timeout=180,
            )
            assert index_update.returncode == 0, index_update.stderr or index_update.stdout

            archivebox_server = start_archivebox_server(
                initialized_archive,
                port=archivebox_port,
                log_name="search-matrix-server.log",
                env=env,
            )
            wait_for_http(
                archivebox_port,
                host=f"public.archivebox.localhost:{archivebox_port}",
                path="/public/",
                process=archivebox_server,
            )
            wait_for_http(
                archivebox_port,
                host=f"admin.archivebox.localhost:{archivebox_port}",
                path="/admin/login/",
                process=archivebox_server,
            )

            for backend_name in ("ripgrep", "sqlite", "sonic"):
                backend_result = run_archivebox_cmd(
                    ["list", "--search=contents", "--csv=url", "public admin search matrix"],
                    cwd=initialized_archive,
                    env={**env, "SEARCH_BACKEND_ENGINE": backend_name},
                    timeout=60,
                )
                assert backend_result.returncode == 0, backend_result.stderr or backend_result.stdout
                backend_urls = [line.strip().strip('"') for line in backend_result.stdout.splitlines() if line.strip()]
                assert set(backend_urls) == set(matrix_urls), (backend_name, backend_result.stdout)

            session = requests.Session()
            login_page = session.get(
                f"http://127.0.0.1:{archivebox_port}/admin/login/",
                headers={"Host": f"admin.archivebox.localhost:{archivebox_port}"},
                timeout=10,
            )
            assert login_page.status_code == 200
            csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login_page.text)
            assert csrf_match, login_page.text[:500]
            login_response = session.post(
                f"http://127.0.0.1:{archivebox_port}/admin/login/",
                headers={
                    "Host": f"admin.archivebox.localhost:{archivebox_port}",
                    "Referer": f"http://admin.archivebox.localhost:{archivebox_port}/admin/login/",
                },
                data={
                    "username": "apitestadmin",
                    "password": "testpass123",
                    "csrfmiddlewaretoken": csrf_match.group(1),
                    "next": "/admin/core/snapshot/",
                },
                timeout=10,
                allow_redirects=False,
            )
            assert login_response.status_code in (302, 303), login_response.text

            public_default = requests.get(
                f"http://127.0.0.1:{archivebox_port}/public/",
                headers={"Host": f"public.archivebox.localhost:{archivebox_port}"},
                timeout=10,
            )
            assert public_default.status_code == 200
            assert "archivebox-search-stream-status" in public_default.text
            assert 'value="deep:sonic" selected' in public_default.text
            assert "No snapshots found." not in public_default.text
            assert "127.0.0.1" in public_default.text

            admin_default = session.get(
                f"http://127.0.0.1:{archivebox_port}/admin/core/snapshot/",
                headers={"Host": f"admin.archivebox.localhost:{archivebox_port}"},
                timeout=10,
            )
            assert admin_default.status_code == 200
            assert "archivebox-search-stream-status" in admin_default.text
            assert 'value="deep:sonic" selected' in admin_default.text
            assert "127.0.0.1" in admin_default.text

            for surface_name, host, stream_path, list_path, requester in (
                (
                    "public",
                    f"public.archivebox.localhost:{archivebox_port}",
                    "/public/search-stream/",
                    "/public/",
                    requests,
                ),
                (
                    "admin",
                    f"admin.archivebox.localhost:{archivebox_port}",
                    "/admin/core/snapshot/search-stream/",
                    "/admin/core/snapshot/",
                    session,
                ),
            ):
                for search_mode, query in (
                    ("meta", "search-matrix"),
                    ("deep:ripgrep", "public admin search matrix"),
                    ("deep:sqlite", "public admin search matrix"),
                    ("deep:sonic", "public admin search matrix"),
                ):
                    params = {"q": query, "search_mode": search_mode}
                    search_url = f"{list_path}?{urlencode(params)}"
                    stream_started = time.monotonic()
                    stream_response = requester.get(
                        f"http://127.0.0.1:{archivebox_port}{stream_path}",
                        headers={"Host": host},
                        params={**params, "search_url": search_url},
                        stream=True,
                        timeout=(5, 30),
                    )
                    assert stream_response.status_code == 200, stream_response.text[:500]
                    assert stream_response.headers.get("X-Accel-Buffering") == "no"

                    counts = []
                    first_positive_elapsed = None
                    for line in stream_response.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        count = int(line.strip())
                        counts.append(count)
                        if count > 0 and first_positive_elapsed is None:
                            first_positive_elapsed = time.monotonic() - stream_started

                    total_elapsed = time.monotonic() - stream_started
                    assert counts[0] == 0, (surface_name, search_mode, counts[:10])
                    assert counts[1] == 1, (surface_name, search_mode, counts[:10])
                    expected_count = total_snapshot_count if search_mode == "meta" else page_count
                    expected_elapsed = 2.0 if expected_count == total_snapshot_count else 1.0
                    assert counts[-1] == expected_count, (surface_name, search_mode, counts[-10:])
                    assert counts == sorted(counts), (surface_name, search_mode, counts[:20])
                    assert any(1 < count < page_count for count in counts), (surface_name, search_mode, counts)
                    assert first_positive_elapsed is not None and first_positive_elapsed < 0.25, (
                        surface_name,
                        search_mode,
                        first_positive_elapsed,
                        counts[:10],
                    )
                    assert total_elapsed < expected_elapsed, (surface_name, search_mode, total_elapsed, counts[-10:])

                    rendered_page = requester.get(
                        f"http://127.0.0.1:{archivebox_port}{list_path}",
                        headers={"Host": host},
                        params=params,
                        timeout=10,
                    )
                    assert rendered_page.status_code == 200
                    assert "No snapshots found." not in rendered_page.text
                    assert "127.0.0.1" in rendered_page.text
                    assert search_mode in rendered_page.text

                    cleared_page = requester.get(
                        f"http://127.0.0.1:{archivebox_port}{list_path}",
                        headers={"Host": host},
                        timeout=10,
                    )
                    assert cleared_page.status_code == 200
                    assert "No snapshots found." not in cleared_page.text
                    assert "127.0.0.1" in cleared_page.text

                for query, expected_url, absent_urls in (
                    ("url-only-meta-needle", url_only_url, (title_only_url, tag_only_url)),
                    (title_only_needle, title_only_url, (url_only_url, tag_only_url)),
                    (tag_only_needle, tag_only_url, (url_only_url, title_only_url)),
                ):
                    params = {"q": query, "search_mode": "meta"}
                    search_url = f"{list_path}?{urlencode(params)}"
                    stream_response = requester.get(
                        f"http://127.0.0.1:{archivebox_port}{stream_path}",
                        headers={"Host": host},
                        params={**params, "search_url": search_url},
                        stream=True,
                        timeout=(5, 30),
                    )
                    assert stream_response.status_code == 200, stream_response.text[:500]
                    counts = [int(line.strip()) for line in stream_response.iter_lines(decode_unicode=True) if line]
                    assert counts[-1] == 1, (surface_name, query, counts)

                    rendered_page = requester.get(
                        f"http://127.0.0.1:{archivebox_port}{list_path}",
                        headers={"Host": host},
                        params=params,
                        timeout=10,
                    )
                    assert rendered_page.status_code == 200
                    assert expected_url in rendered_page.text
                    for absent_url in absent_urls:
                        assert absent_url not in rendered_page.text

                params = {"q": order_needle, "search_mode": "meta"}
                search_url = f"{list_path}?{urlencode(params)}"
                stream_response = requester.get(
                    f"http://127.0.0.1:{archivebox_port}{stream_path}",
                    headers={"Host": host},
                    params={**params, "search_url": search_url},
                    stream=True,
                    timeout=(5, 30),
                )
                assert stream_response.status_code == 200, stream_response.text[:500]
                counts = [int(line.strip()) for line in stream_response.iter_lines(decode_unicode=True) if line]
                assert counts[-1] == 4, (surface_name, order_needle, counts)

                rendered_page = requester.get(
                    f"http://127.0.0.1:{archivebox_port}{list_path}",
                    headers={"Host": host},
                    params=params,
                    timeout=10,
                )
                assert rendered_page.status_code == 200
                rendered_text = rendered_page.text
                assert rendered_text.index(title_prefix_order_url) < rendered_text.index(url_contains_order_url)
                assert rendered_text.index(url_contains_order_url) < rendered_text.index(title_contains_order_url)
                assert rendered_text.index(title_contains_order_url) < rendered_text.index(tag_order_url)
        finally:
            if archivebox_server is not None:
                stop_archivebox_process(archivebox_server)
            fixture_server.shutdown()
            fixture_server.server_close()
            fixture_thread.join(timeout=5)
