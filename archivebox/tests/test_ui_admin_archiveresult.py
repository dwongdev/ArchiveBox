"""ArchiveResult admin UI tests."""

import pytest
from django.urls import reverse

from archivebox.tests.conftest import ADMIN_TEST_HOST

pytestmark = pytest.mark.django_db


class TestArchiveResultAdminListView:
    def test_list_view_renders_readonly_tags_and_noresults_status(self, client, admin_user, snapshot):
        from archivebox.core.models import ArchiveResult, Tag

        tag = Tag.objects.create(name="Alpha Research")
        snapshot.tags.add(tag)
        ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin="title",
            status=ArchiveResult.StatusChoices.NORESULTS,
            output_str="No title found",
        )

        client.force_login(admin_user)
        response = client.get(reverse("admin:core_archiveresult_changelist"), HTTP_HOST=ADMIN_TEST_HOST)

        assert response.status_code == 200
        assert b"Alpha Research" in response.content
        assert b"tag-editor-inline readonly" in response.content
        assert b"No Results" in response.content

    def test_archiveresult_model_has_retry_at_field(self):
        from archivebox.core.models import ArchiveResult

        assert "retry_at" in {field.name for field in ArchiveResult._meta.fields}
