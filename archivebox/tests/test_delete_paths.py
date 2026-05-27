import json
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from archivebox.api.auth import get_or_create_api_token
from archivebox.core.models import Snapshot
from archivebox.crawls.models import Crawl


pytestmark = pytest.mark.django_db(transaction=True)

API_HOST = "api.archivebox.localhost:8000"


def _api_headers(token):
    return {
        "HTTP_HOST": API_HOST,
        "HTTP_X_ARCHIVEBOX_API_KEY": token.token,
    }


def _admin_token():
    user = get_user_model().objects.create_superuser(
        username="deletepathadmin",
        email="deletepathadmin@test.com",
        password="testpassword",
    )
    token = get_or_create_api_token(user)
    assert token is not None
    return token


def test_rest_snapshot_delete_removes_output_dir(client):
    token = _admin_token()
    url = "https://example.com/delete-path-snapshot"

    response = client.post(
        "/api/v1/core/snapshots",
        data=json.dumps({"url": url, "depth": 0, "status": Snapshot.StatusChoices.QUEUED}),
        content_type="application/json",
        **_api_headers(token),
    )
    assert response.status_code == 200, response.content.decode()

    snapshot = Snapshot.objects.get(url=url)
    snapshot_dir = Path(snapshot.output_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "delete-path-test.txt").write_text("snapshot output")
    assert snapshot_dir.exists()

    response = client.delete(f"/api/v1/core/snapshot/{snapshot.id}", **_api_headers(token))
    assert response.status_code == 200, response.content.decode()
    assert not Snapshot.objects.filter(pk=snapshot.pk).exists()
    assert not snapshot_dir.exists()


def test_rest_crawl_delete_removes_crawl_and_snapshot_output_dirs(client):
    token = _admin_token()
    url = "https://example.com/delete-path-crawl"

    response = client.post(
        "/api/v1/crawls/crawls",
        data=json.dumps({"urls": [url], "max_depth": 0, "max_urls": 1}),
        content_type="application/json",
        **_api_headers(token),
    )
    assert response.status_code == 200, response.content.decode()

    crawl = Crawl.objects.get(urls__contains=url)
    snapshot = crawl.snapshot_set.get(url=url)
    crawl_dir = Path(crawl.output_dir)
    snapshot_dir = Path(snapshot.output_dir)
    crawl_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (crawl_dir / "delete-path-crawl.txt").write_text("crawl output")
    (snapshot_dir / "delete-path-snapshot.txt").write_text("snapshot output")
    assert crawl_dir.exists()
    assert snapshot_dir.exists()

    response = client.delete(f"/api/v1/crawls/crawl/{crawl.id}", **_api_headers(token))
    assert response.status_code == 200, response.content.decode()
    assert not Crawl.objects.filter(pk=crawl.pk).exists()
    assert not Snapshot.objects.filter(pk=snapshot.pk).exists()
    assert not crawl_dir.exists()
    assert not snapshot_dir.exists()
