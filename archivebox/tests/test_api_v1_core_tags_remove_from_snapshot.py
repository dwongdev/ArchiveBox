import pytest

from archivebox.core.models import Snapshot, Tag
from archivebox.crawls.models import Crawl
from archivebox.tests.conftest import api_client_request


pytestmark = pytest.mark.django_db(transaction=True)


def test_basic_success_case_request(client, api_admin_user, api_headers):
    crawl = Crawl.objects.create(urls="https://example.com/tag-remove", created_by=api_admin_user)
    snapshot = Snapshot.objects.create(url="https://example.com/tag-remove", crawl=crawl)
    tag = Tag.objects.create(name="api-basic-remove-tag", created_by=api_admin_user)
    snapshot.tags.add(tag)

    response = api_client_request(
        client,
        "post",
        "/api/v1/core/tags/remove-from-snapshot/",
        payload={"snapshot_id": str(snapshot.id), "tag_id": tag.id},
        headers=api_headers,
    )

    assert response.status_code == 200, response.content
    assert response.json()["success"] is True
