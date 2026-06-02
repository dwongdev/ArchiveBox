"""Tag admin UI and API tests."""

import pytest
from django.urls import reverse

from archivebox.tests.conftest import ADMIN_TEST_HOST

pytestmark = pytest.mark.django_db


def test_tag_admin_changelist_renders_custom_ui(admin_client, tagged_data):
    response = admin_client.get(reverse("admin:core_tag_changelist"), HTTP_HOST=ADMIN_TEST_HOST)

    assert response.status_code == 200
    assert b'id="tag-live-search"' in response.content
    assert b'id="tag-sort-select"' in response.content
    assert b'id="tag-created-by-select"' in response.content
    assert b'id="tag-year-select"' in response.content
    assert b"Alpha Research" in response.content
    assert b'class="tag-card"' in response.content


def test_tag_admin_add_view_renders_similar_tag_reference(admin_client):
    response = admin_client.get(reverse("admin:core_tag_add"), HTTP_HOST=ADMIN_TEST_HOST)

    assert response.status_code == 200
    assert b"Similar Tags" in response.content
    assert b'data-tag-name-input="1"' in response.content


# test_tag_search_api_returns_card_payload moved to test_api_v1_core_tags_search.py.
# test_tag_search_api_respects_sort_and_filters moved to test_api_v1_core_tags_search.py.
# test_tag_rename_api_updates_name moved to test_api_v1_core_tag_tag_id_rename.py.
# test_tag_snapshots_export_returns_jsonl moved to test_api_v1_core_tag_tag_id_snapshots_jsonl.py.
# test_tag_urls_export_returns_plain_text_urls moved to test_api_v1_core_tag_tag_id_urls_txt.py.
