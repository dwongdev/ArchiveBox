import re
from typing import cast

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import UserManager
from django.urls import reverse

from archivebox.crawls.admin import CrawlAdminForm
from archivebox.crawls.models import Crawl
from archivebox.core.models import Snapshot


pytestmark = pytest.mark.django_db


User = get_user_model()
ADMIN_HOST = "admin.archivebox.localhost:8000"


@pytest.fixture
def admin_user(db):
    return cast(UserManager, User.objects).create_superuser(
        username="crawladmin",
        email="crawladmin@test.com",
        password="testpassword",
    )


@pytest.fixture
def crawl(admin_user):
    return Crawl.objects.create(
        urls="https://example.com\nhttps://example.org",
        tags_str="alpha,beta",
        created_by=admin_user,
    )


def test_crawl_admin_change_view_renders_tag_editor_widget(client, admin_user, crawl):
    client.login(username="crawladmin", password="testpassword")

    response = client.get(
        reverse("admin:crawls_crawl_change", args=[crawl.pk]),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    assert b'name="tags_editor"' in response.content
    assert b"tag-editor-container" in response.content
    assert b"alpha" in response.content
    assert b"beta" in response.content


def test_crawl_admin_add_view_renders_url_filter_alias_fields(client, admin_user):
    client.login(username="crawladmin", password="testpassword")

    response = client.get(
        reverse("admin:crawls_crawl_add"),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    assert b'name="url_filters_allowlist"' in response.content
    assert b'name="url_filters_denylist"' in response.content
    assert b"Same domain only" in response.content
    assert b"Subpaths only" in response.content


def test_crawl_admin_change_view_checks_effective_only_new(client, admin_user):
    crawl = Crawl.objects.create(
        urls="https://example.com",
        config={},
        created_by=admin_user,
    )
    client.login(username="crawladmin", password="testpassword")

    response = client.get(
        reverse("admin:crawls_crawl_change", args=[crawl.pk]),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    assert b"Effective ONLY_NEW" not in response.content
    assert b'id="id_url_filters_only_new" name="url_filters_only_new" value="1" checked' in response.content


def test_crawl_admin_change_view_derives_url_filter_shortcut_toggles(client, admin_user):
    crawl = Crawl.objects.create(
        urls="https://example.com/docs/page.html",
        config={"URL_ALLOWLIST": r"^https?://example\.com/docs/"},
        created_by=admin_user,
    )
    client.login(username="crawladmin", password="testpassword")

    response = client.get(
        reverse("admin:crawls_crawl_change", args=[crawl.pk]),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    assert b'id="id_url_filters_same_domain_only" name="url_filters_same_domain_only" value="1" checked' in response.content
    assert b'id="id_url_filters_subpaths_only" name="url_filters_subpaths_only" value="1" checked' in response.content


def test_admin_change_submit_row_uses_single_save_continue_button(client, admin_user, crawl):
    client.login(username="crawladmin", password="testpassword")

    response = client.get(
        reverse("admin:crawls_crawl_change", args=[crawl.pk]),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    submit_rows = re.findall(r'<div class="submit-row">.*?</div>', response.content.decode(), flags=re.DOTALL)
    assert submit_rows
    for row in submit_rows:
        assert 'name="_save"' not in row
        assert 'name="_addanother"' not in row
        assert 'value="Save and continue editing"' not in row
        assert 'value="Save"' in row
        assert 'name="_continue"' in row


def test_admin_add_submit_row_hides_save_and_add_another(client, admin_user):
    client.login(username="crawladmin", password="testpassword")

    response = client.get(
        reverse("admin:crawls_crawl_add"),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    submit_rows = re.findall(r'<div class="submit-row">.*?</div>', response.content.decode(), flags=re.DOTALL)
    assert submit_rows
    assert all('name="_addanother"' not in row for row in submit_rows)


def test_crawl_schedule_admin_add_redirects_to_add_page_schedule_field(client, admin_user):
    client.login(username="crawladmin", password="testpassword")

    response = client.get(reverse("admin:crawls_crawlschedule_add"), HTTP_HOST=ADMIN_HOST)

    assert response.status_code == 302
    assert response["Location"] == "/add/#schedule"


def test_crawl_admin_form_saves_tags_editor_to_tags_str(crawl, admin_user):
    form = CrawlAdminForm(
        data={
            "created_at": crawl.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "urls": crawl.urls,
            "config": '{"CRAWL_MAX_URLS": 3, "CRAWL_MAX_SIZE": 47185920, "CRAWL_TIMEOUT": 120, "SNAPSHOT_MAX_SIZE": 5242880}',
            "max_depth": "0",
            "tags_editor": "alpha, beta, Alpha, gamma",
            "url_filters_allowlist": "example.com\n*.example.com",
            "url_filters_denylist": "static.example.com",
            "persona_id": "",
            "label": "",
            "notes": "",
            "schedule": "",
            "status": crawl.status,
            "retry_at": crawl.retry_at.strftime("%Y-%m-%d %H:%M:%S"),
            "created_by": str(admin_user.pk),
            "num_uses_failed": "0",
            "num_uses_succeeded": "0",
        },
        instance=crawl,
    )

    assert form.is_valid(), form.errors

    updated = form.save()
    updated.refresh_from_db()
    assert updated.tags_str == "alpha,beta,gamma"
    assert updated.config["CRAWL_MAX_URLS"] == 3
    assert updated.config["CRAWL_MAX_SIZE"] == 45 * 1024 * 1024
    assert updated.config["CRAWL_TIMEOUT"] == 120
    assert updated.config["SNAPSHOT_MAX_SIZE"] == 5 * 1024 * 1024
    assert updated.config["URL_ALLOWLIST"] == "example.com\n*.example.com"
    assert updated.config["URL_DENYLIST"] == "static.example.com"


def test_crawl_admin_resume_action_updates_only_status(client, admin_user, crawl):
    crawl.status = Crawl.StatusChoices.SEALED
    crawl.retry_at = None
    crawl.notes = "unsaved-change-guard"
    crawl.save(update_fields=["status", "retry_at", "notes", "modified_at"])

    client.login(username="crawladmin", password="testpassword")
    response = client.post(
        reverse("admin:crawls_crawl_changelist"),
        data={
            "action": "resume_selected_crawls",
            "_selected_action": str(crawl.pk),
            "index": "0",
        },
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 302
    crawl.refresh_from_db()
    assert crawl.status == Crawl.StatusChoices.QUEUED
    assert crawl.retry_at is not None
    assert crawl.notes == "unsaved-change-guard"


def test_crawl_admin_pause_action_updates_only_crawl_scheduler_row(client, admin_user, crawl):
    snapshots = crawl.create_snapshots_from_urls()
    client.login(username="crawladmin", password="testpassword")

    response = client.post(
        reverse("admin:crawls_crawl_changelist"),
        data={
            "action": "pause_selected_crawls",
            "_selected_action": str(crawl.pk),
            "index": "0",
        },
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 302
    crawl.refresh_from_db()
    assert crawl.status == Crawl.StatusChoices.PAUSED
    assert list(Snapshot.objects.filter(pk__in=[snapshot.pk for snapshot in snapshots]).values_list("status", flat=True)) == [
        Snapshot.StatusChoices.QUEUED,
        Snapshot.StatusChoices.QUEUED,
    ]


@pytest.mark.django_db(transaction=True)
def test_crawl_tag_changes_sync_existing_snapshot_tags(crawl):
    snapshots = crawl.create_snapshots_from_urls()
    snapshots[0].save_tags(["alpha", "beta", "keep"])

    crawl.tags_str = "beta,gamma"
    crawl.save(update_fields=["tags_str", "modified_at"])

    assert set(snapshots[0].tags.values_list("name", flat=True)) == {"beta", "gamma", "keep"}
    assert set(snapshots[1].tags.values_list("name", flat=True)) == {"beta", "gamma"}


@pytest.mark.django_db(transaction=True)
def test_create_snapshots_from_urls_uses_current_crawl_tags_for_stale_crawl_instance(crawl):
    crawl.create_snapshots_from_urls()
    fresh_crawl = Crawl.objects.get(pk=crawl.pk)
    fresh_crawl.tags_str = "midcrawl"
    fresh_crawl.save(update_fields=["tags_str", "modified_at"])

    crawl.urls = f"{crawl.urls}\nhttps://example.net/new"
    created = crawl.create_snapshots_from_urls()

    assert [snapshot.url for snapshot in created] == ["https://example.net/new"]
    assert set(created[0].tags.values_list("name", flat=True)) == {"midcrawl"}


@pytest.mark.django_db(transaction=True)
def test_discovered_snapshots_inherit_current_crawl_tags(crawl):
    crawl.max_depth = 1
    crawl.save(update_fields=["max_depth", "modified_at"])
    parent_snapshot = crawl.create_snapshots_from_urls()[0]
    crawl.tags_str = "midcrawl"
    crawl.save(update_fields=["tags_str", "modified_at"])

    created = crawl.create_discovered_snapshots(
        parent_snapshot,
        [{"url": "https://example.com/child", "tags": "discovered"}],
        depth=1,
    )

    assert [snapshot.url for snapshot in created] == ["https://example.com/child"]
    assert set(created[0].tags.values_list("name", flat=True)) == {"midcrawl", "discovered"}


def test_crawl_admin_delete_snapshot_action_removes_snapshot_and_url(client, admin_user):
    crawl = Crawl.objects.create(
        urls="https://example.com/remove-me",
        created_by=admin_user,
    )
    snapshot = Snapshot.objects.create(
        crawl=crawl,
        url="https://example.com/remove-me",
    )

    client.login(username="crawladmin", password="testpassword")
    response = client.post(
        reverse("admin:crawls_crawl_snapshot_delete", args=[crawl.pk, snapshot.pk]),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert not Snapshot.objects.filter(pk=snapshot.pk).exists()

    crawl.refresh_from_db()
    assert "https://example.com/remove-me" not in crawl.urls


def test_crawl_admin_exclude_domain_action_prunes_urls_and_pending_snapshots(client, admin_user):
    crawl = Crawl.objects.create(
        urls="\n".join(
            [
                "https://cdn.example.com/asset.js",
                "https://cdn.example.com/second.js",
                "https://example.com/root",
            ],
        ),
        created_by=admin_user,
    )
    queued_snapshot = Snapshot.objects.create(
        crawl=crawl,
        url="https://cdn.example.com/asset.js",
        status=Snapshot.StatusChoices.QUEUED,
    )
    preserved_snapshot = Snapshot.objects.create(
        crawl=crawl,
        url="https://example.com/root",
        status=Snapshot.StatusChoices.SEALED,
    )

    client.login(username="crawladmin", password="testpassword")
    response = client.post(
        reverse("admin:crawls_crawl_snapshot_exclude_domain", args=[crawl.pk, queued_snapshot.pk]),
        HTTP_HOST=ADMIN_HOST,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["domain"] == "cdn.example.com"

    crawl.refresh_from_db()
    assert "cdn.example.com" in crawl.get_url_denylist(use_effective_config=False)
    assert "https://cdn.example.com/asset.js" not in crawl.urls
    assert "https://cdn.example.com/second.js" not in crawl.urls
    assert "https://example.com/root" in crawl.urls
    assert not Snapshot.objects.filter(pk=queued_snapshot.pk).exists()
    assert Snapshot.objects.filter(pk=preserved_snapshot.pk).exists()


def test_snapshot_from_json_trims_markdown_suffixes_on_discovered_urls(crawl):
    snapshot = Snapshot.from_json(
        {"url": "https://docs.sweeting.me/s/youtube-favorites)**"},
        overrides={"crawl": crawl},
        queue_for_extraction=False,
    )

    assert snapshot is not None
    assert snapshot.url == "https://docs.sweeting.me/s/youtube-favorites"


def test_create_snapshots_from_urls_respects_url_allowlist_and_denylist(admin_user):
    crawl = Crawl.objects.create(
        urls="\n".join(
            [
                "https://example.com/root",
                "https://static.example.com/app.js",
                "https://other.test/page",
            ],
        ),
        created_by=admin_user,
        config={
            "URL_ALLOWLIST": "example.com",
            "URL_DENYLIST": "static.example.com",
        },
    )

    created = crawl.create_snapshots_from_urls()

    assert [snapshot.url for snapshot in created] == ["https://example.com/root"]


def test_create_snapshots_from_urls_skips_invalid_and_archivebox_internal_urls(admin_user):
    crawl = Crawl.objects.create(
        urls="\n".join(
            [
                "https://example.com/root",
                "http://127.0.0.1:8765/page-001.html",
                "not-a-url",
                "http://admin.archivebox.localhost:8000/admin/",
            ],
        ),
        created_by=admin_user,
    )

    created = crawl.create_snapshots_from_urls()

    assert [snapshot.url for snapshot in created] == [
        "https://example.com/root",
        "http://127.0.0.1:8765/page-001.html",
    ]
    assert list(crawl.snapshot_set.order_by("created_at").values_list("url", flat=True)) == [
        "https://example.com/root",
        "http://127.0.0.1:8765/page-001.html",
    ]


def test_create_snapshots_from_urls_respects_max_urls(admin_user):
    crawl = Crawl.objects.create(
        urls="\n".join(
            [
                "https://example.com/root",
                "https://example.com/about",
                "https://example.com/contact",
            ],
        ),
        config={"CRAWL_MAX_URLS": 2},
        created_by=admin_user,
    )

    created = crawl.create_snapshots_from_urls()

    assert [snapshot.url for snapshot in created] == [
        "https://example.com/root",
        "https://example.com/about",
    ]
    assert crawl.snapshot_set.count() == 2
    assert crawl.remaining_snapshot_capacity() == 0
    assert crawl.limit_stop_reason() == "crawl_max_urls"
    assert crawl.add_url({"url": "https://example.com/extra", "depth": 1}) is False


def test_crawl_stop_reason_reports_no_viable_urls_for_sealed_empty_crawl(admin_user):
    crawl = Crawl.objects.create(
        urls="https://example.com/already-known",
        status=Crawl.StatusChoices.SEALED,
        retry_at=None,
        created_by=admin_user,
    )

    assert crawl.stop_reason() == "no_viable_urls"


def test_crawl_stop_reason_reports_done_for_sealed_crawl_with_all_snapshots_sealed(admin_user):
    crawl = Crawl.objects.create(
        urls="https://example.com/done",
        status=Crawl.StatusChoices.SEALED,
        retry_at=None,
        created_by=admin_user,
    )
    Snapshot.objects.create(
        url="https://example.com/done",
        crawl=crawl,
        status=Snapshot.StatusChoices.SEALED,
        timestamp="1700000000.010",
    )

    assert crawl.stop_reason() == "done"


def test_crawl_stop_reason_reports_paused_for_paused_crawl(admin_user):
    crawl = Crawl.objects.create(
        urls="https://example.com/paused",
        status=Crawl.StatusChoices.PAUSED,
        created_by=admin_user,
    )

    assert crawl.stop_reason() == "paused"


def test_crawl_stop_reason_keeps_specific_limit_reason_over_lifecycle_fallback(admin_user):
    crawl = Crawl.objects.create(
        urls="\n".join(
            [
                "https://example.com/root",
                "https://example.com/about",
            ],
        ),
        config={"CRAWL_MAX_URLS": 1},
        status=Crawl.StatusChoices.SEALED,
        retry_at=None,
        created_by=admin_user,
    )
    Snapshot.objects.create(
        url="https://example.com/root",
        crawl=crawl,
        status=Snapshot.StatusChoices.SEALED,
        timestamp="1700000000.011",
    )

    assert crawl.stop_reason() == "crawl_max_urls"


def test_create_snapshots_from_urls_respects_only_new_exact_url_matches(admin_user):
    existing_crawl = Crawl.objects.create(urls="https://example.com/existing", created_by=admin_user)
    Snapshot.objects.create(
        url="https://example.com/existing",
        crawl=existing_crawl,
        timestamp="1700000000.001",
    )
    crawl = Crawl.objects.create(
        urls="\n".join(
            [
                "https://example.com/existing",
                "https://example.com/existing/",
                "https://example.com/fresh",
            ],
        ),
        config={"ONLY_NEW": True},
        created_by=admin_user,
    )

    created = crawl.create_snapshots_from_urls()

    assert [snapshot.url for snapshot in created] == [
        "https://example.com/existing/",
        "https://example.com/fresh",
    ]
    assert Snapshot.objects.filter(url="https://example.com/existing").count() == 1


def test_create_snapshots_from_urls_allows_existing_exact_url_when_only_new_false(admin_user):
    existing_crawl = Crawl.objects.create(urls="https://example.com/existing", created_by=admin_user)
    Snapshot.objects.create(
        url="https://example.com/existing",
        crawl=existing_crawl,
        timestamp="1700000000.002",
    )
    crawl = Crawl.objects.create(
        urls="https://example.com/existing",
        config={"ONLY_NEW": False},
        created_by=admin_user,
    )

    created = crawl.create_snapshots_from_urls()

    assert [snapshot.url for snapshot in created] == ["https://example.com/existing"]
    assert Snapshot.objects.filter(url="https://example.com/existing").count() == 2


def test_create_discovered_snapshots_respects_only_new_exact_url_matches(admin_user):
    existing_crawl = Crawl.objects.create(urls="https://example.com/existing", created_by=admin_user)
    Snapshot.objects.create(
        url="https://example.com/existing",
        crawl=existing_crawl,
        timestamp="1700000000.003",
    )
    crawl = Crawl.objects.create(
        urls="https://example.com/root",
        max_depth=1,
        config={"ONLY_NEW": True},
        created_by=admin_user,
    )
    parent = crawl.create_snapshots_from_urls()[0]

    created = crawl.create_discovered_snapshots(
        parent,
        [
            {"url": "https://example.com/existing"},
            {"url": "https://example.com/existing/"},
            {"url": "https://example.com/fresh"},
        ],
        depth=1,
    )

    assert [snapshot.url for snapshot in created] == [
        "https://example.com/existing/",
        "https://example.com/fresh",
    ]
    assert Snapshot.objects.filter(url="https://example.com/existing").count() == 1


def test_url_filter_regex_lists_preserve_commas_and_split_on_newlines_only(admin_user):
    crawl = Crawl.objects.create(
        urls="\n".join(
            [
                "https://example.com/root",
                "https://example.com/path,with,commas",
                "https://other.test/page",
            ],
        ),
        created_by=admin_user,
        config={
            "URL_ALLOWLIST": r"^https://example\.com/(root|path,with,commas)$" + "\n" + r"^https://other\.test/page$",
            "URL_DENYLIST": r"^https://example\.com/path,with,commas$",
        },
    )

    assert crawl.get_url_allowlist(use_effective_config=False) == [
        r"^https://example\.com/(root|path,with,commas)$",
        r"^https://other\.test/page$",
    ]
    assert crawl.get_url_denylist(use_effective_config=False) == [
        r"^https://example\.com/path,with,commas$",
    ]

    created = crawl.create_snapshots_from_urls()

    assert [snapshot.url for snapshot in created] == [
        "https://example.com/root",
        "https://other.test/page",
    ]
