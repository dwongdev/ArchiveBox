import pytest

from archivebox.tests.conftest import api_client_request


pytestmark = pytest.mark.django_db(transaction=True)


def test_basic_success_case_request(client, api_headers):
    response = api_client_request(
        client,
        "post",
        "/api/v1/personas/sync",
        payload={
            "extension_persona_id": "extension-api-persona-basic",
            "name": "api-persona-basic",
            "settings": {},
            "cookies_txt": "",
            "auth_json": {},
        },
        headers=api_headers,
    )

    assert response.status_code == 200, response.content
    assert response.json()["success"] is True
