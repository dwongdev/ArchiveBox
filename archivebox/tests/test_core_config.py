from archivebox.config import CONSTANTS


def test_sonic_dir_is_allowed_inside_data_dir():
    assert "sonic" in CONSTANTS.ALLOWED_IN_DATA_DIR
