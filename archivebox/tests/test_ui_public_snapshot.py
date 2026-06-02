"""Public snapshot UI tests."""

import pytest
from django.test import override_settings

from archivebox.tests.conftest import PUBLIC_TEST_HOST, WEB_TEST_HOST

pytestmark = pytest.mark.django_db


class TestPublicIndex:
    """Tests for public index visibility and redirects."""

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_lists_only_public_snapshots(self, client, admin_user):
        from archivebox.core.models import Snapshot
        from archivebox.crawls.models import Crawl

        public_crawl = Crawl.objects.create(urls="https://public.example", created_by=admin_user, config={"PERMISSIONS": "public"})
        unlisted_crawl = Crawl.objects.create(urls="https://unlisted.example", created_by=admin_user, config={"PERMISSIONS": "unlisted"})
        private_crawl = Crawl.objects.create(urls="https://private.example", created_by=admin_user, config={"PERMISSIONS": "private"})
        Snapshot.objects.create(
            url="https://public.example",
            title="Public Snapshot",
            crawl=public_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        Snapshot.objects.create(
            url="https://unlisted.example",
            title="Unlisted Snapshot",
            crawl=unlisted_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        Snapshot.objects.create(
            url="https://private.example",
            title="Private Snapshot",
            crawl=private_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )

        response = client.get("/public/", HTTP_HOST=PUBLIC_TEST_HOST)

        assert response.status_code == 200
        assert b"Public Snapshot" in response.content
        assert b"Unlisted Snapshot" not in response.content
        assert b"Private Snapshot" not in response.content

    def test_direct_snapshot_urls_allow_unlisted_but_not_private_for_guests(self, client, admin_user):
        from archivebox.core.models import Snapshot
        from archivebox.crawls.models import Crawl

        unlisted_crawl = Crawl.objects.create(urls="https://unlisted.example", created_by=admin_user, config={"PERMISSIONS": "unlisted"})
        private_crawl = Crawl.objects.create(urls="https://private.example", created_by=admin_user, config={"PERMISSIONS": "private"})
        unlisted_snapshot = Snapshot.objects.create(
            url="https://unlisted.example",
            crawl=unlisted_crawl,
            status=Snapshot.StatusChoices.SEALED,
        )
        private_snapshot = Snapshot.objects.create(url="https://private.example", crawl=private_crawl, status=Snapshot.StatusChoices.SEALED)

        unlisted_response = client.get(f"/snapshot/{unlisted_snapshot.id}/", HTTP_HOST=WEB_TEST_HOST)
        private_response = client.get(f"/snapshot/{private_snapshot.id}/", HTTP_HOST=WEB_TEST_HOST)

        assert unlisted_response.status_code == 200
        assert private_response.status_code == 302
        assert private_response["Location"].startswith("/admin/login/")

    @override_settings(PUBLIC_INDEX=True)
    def test_public_index_redirects_logged_in_users_to_admin_snapshot_list(self, client, admin_user):
        client.force_login(admin_user)

        response = client.get("/public/", HTTP_HOST=PUBLIC_TEST_HOST)

        assert response.status_code == 302
        assert response["Location"] == "/admin/core/snapshot/"
