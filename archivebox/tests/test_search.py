import os
import signal
import socket
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import urlencode
from archivebox.tests.conftest import run_archivebox_cmd

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.test.client import RequestFactory
from django.urls import reverse

from archivebox.misc.logging import AttrDict


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
        assert b'value="contents"' in response.content
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
        assert cached["ids"][:3] == [str(title_snapshot.pk), str(contains_snapshot.pk), str(prefix_snapshot.pk)]
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
        response = client.get("/public/", {"q": "public-example.com"}, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_mode_selector_defaults_to_configured_deep_backend_for_ripgrep(self, client, monkeypatch):
        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")

        response = client.get("/public/", HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200
        assert response.context["search_mode"] == "deep:ripgrep"
        assert b'name="search_mode"' in response.content
        assert b'value="deep:ripgrep"' in response.content
        assert b">deep:ripgrep<" in response.content

    def test_public_search_ranks_metadata_matches_before_fulltext(self, crawl, monkeypatch):
        from archivebox.core.models import Snapshot
        from archivebox.core.views import PublicIndexView

        monkeypatch.setenv("SEARCH_BACKEND_ENGINE", "ripgrep")
        metadata_snapshot = Snapshot.objects.create(
            url="https://public-example.com/google-meta",
            title="Google Metadata Match",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        fulltext_snapshot = Snapshot.objects.create(
            url="https://public-example.com/fulltext-only",
            title="Unrelated Title",
            crawl=crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        output_file = fulltext_snapshot.output_dir / "dom" / "output.html"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("<html><body>google public fulltext match</body></html>", encoding="utf-8")

        request = RequestFactory().get("/public/", {"q": "google", "search_mode": "contents"})
        view = PublicIndexView()
        view.request = request

        result_ids = list(view.get_queryset().values_list("pk", flat=True))
        assert metadata_snapshot.pk in result_ids[:2]
        assert fulltext_snapshot.pk in result_ids[:2]

    @override_settings(PUBLIC_INDEX=True)
    def test_public_search_by_title(self, client, public_snapshot):
        response = client.get("/public/", {"q": "Public Example"}, HTTP_HOST=PUBLIC_HOST)

        assert response.status_code == 200

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
    def test_real_fulltext_search_backends_survive_reindex_transition(self, tmp_path):
        data_dir = tmp_path / "archivebox_data"
        data_dir.mkdir()
        query = "documentation examples"

        def free_port() -> int:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])

        def archivebox(*args: str, env: dict[str, str] | None = None, timeout: int = 120):
            merged_env = os.environ.copy()
            merged_env.update(
                {
                    "USE_COLOR": "False",
                    "SHOW_PROGRESS": "False",
                    "SAVE_WARC": "False",
                    "WGET_WARC_ENABLED": "False",
                    "WGET_TIMEOUT": "20",
                },
            )
            if env:
                merged_env.update(env)
            return run_archivebox_cmd(
                list(args),
                cwd=data_dir,
                env=merged_env,
                timeout=timeout,
            )

        init_result = archivebox("init", "--quick", timeout=90)
        assert init_result.returncode == 0, init_result.stderr

        add_result = archivebox("add", "--depth=0", "--plugins=wget", "https://example.com", env={"SEARCH_BACKEND_ENGINE": "ripgrep"})
        assert add_result.returncode == 0, add_result.stderr

        rg_result = archivebox("list", "--search=contents", "--csv=url", query, env={"SEARCH_BACKEND_ENGINE": "ripgrep"})
        assert rg_result.returncode == 0, rg_result.stderr
        assert "https://example.com" in rg_result.stdout

        sqlite_update = archivebox("update", "--index-only", "--batch-size=10", env={"SEARCH_BACKEND_ENGINE": "sqlite"})
        assert sqlite_update.returncode == 0, sqlite_update.stderr
        sqlite_result = archivebox("list", "--search=contents", "--csv=url", query, env={"SEARCH_BACKEND_ENGINE": "sqlite"})
        assert sqlite_result.returncode == 0, sqlite_result.stderr
        assert "https://example.com" in sqlite_result.stdout

        http_port = free_port()
        sonic_port = free_port()
        sonic_env = os.environ.copy()
        sonic_env.update(
            {
                "USE_COLOR": "False",
                "SHOW_PROGRESS": "False",
                "SEARCH_BACKEND_ENGINE": "sonic",
                "SEARCH_BACKEND_SONIC_PORT": str(sonic_port),
            },
        )
        server_log = data_dir / "server.log"
        with server_log.open("w", encoding="utf-8") as log_file:
            server = run_archivebox_cmd(
                ["server", f"127.0.0.1:{http_port}"],
                cwd=data_dir,
                env=sonic_env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                wait=False,
            )
        try:
            for _ in range(80):
                if server.poll() is not None:
                    break
                try:
                    with socket.create_connection(("127.0.0.1", sonic_port), timeout=0.25):
                        break
                except OSError:
                    time.sleep(0.5)
            else:
                raise AssertionError(
                    f"Sonic did not start on port {sonic_port}:\n{server_log.read_text(encoding='utf-8', errors='replace')}",
                )

            sonic_update = archivebox(
                "update",
                "--index-only",
                "--batch-size=10",
                env={
                    "SEARCH_BACKEND_ENGINE": "sonic",
                    "SEARCH_BACKEND_SONIC_PORT": str(sonic_port),
                },
            )
            assert sonic_update.returncode == 0, sonic_update.stderr
            sonic_result = archivebox(
                "list",
                "--search=contents",
                "--csv=url",
                query,
                env={
                    "SEARCH_BACKEND_ENGINE": "sonic",
                    "SEARCH_BACKEND_SONIC_PORT": str(sonic_port),
                },
            )
            assert sonic_result.returncode == 0, sonic_result.stderr
            assert "https://example.com" in sonic_result.stdout
        finally:
            if server.poll() is None:
                os.killpg(server.pid, signal.SIGTERM)
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(server.pid, signal.SIGKILL)
                    server.wait(timeout=10)

            for _ in range(20):
                leftovers = subprocess.run(
                    ["pgrep", "-af", str(data_dir)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                remaining = [line for line in leftovers.stdout.splitlines() if "pgrep -af" not in line]
                if not remaining:
                    break
                time.sleep(0.25)
            else:
                raise AssertionError(f"archivebox server left supervised worker processes running:\n{leftovers.stdout}")
