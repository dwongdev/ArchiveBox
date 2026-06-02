import pytest

from archivebox.machine.models import Binary, Machine


pytestmark = pytest.mark.django_db(transaction=True)


def test_basic_success_case_request(client, tmp_path, api_headers):
    machine = Machine.current(refresh=True)
    binary = Binary.objects.create(
        machine=machine,
        name="api-basic-bin",
        binprovider="env",
        abspath="/usr/bin/env",
        version="1.0",
        status=Binary.StatusChoices.INSTALLED,
    )

    response = client.get(f"/api/v1/machine/binary/{binary.id}", **api_headers)

    assert response.status_code == 200, response.content
