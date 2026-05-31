"""Root pytest bootstrap.

This file is intentionally outside the ``archivebox`` package so pytest can
load it before importing ``archivebox/__init__.py``. ArchiveBox constants are
computed at import time, so DATA_DIR must already point at a temp collection.
"""

import os
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SESSION_DATA_DIR = Path(
    os.environ.get("ARCHIVEBOX_PYTEST_SESSION_DATA_DIR") or tempfile.mkdtemp(prefix="archivebox-pytest-session-"),
).resolve()

os.environ["ARCHIVEBOX_PYTEST_SESSION_DATA_DIR"] = str(SESSION_DATA_DIR)
os.environ["DATA_DIR"] = str(SESSION_DATA_DIR)
(SESSION_DATA_DIR / "tests").mkdir(parents=True, exist_ok=True)
os.chdir(SESSION_DATA_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "archivebox.core.settings")
os.environ.pop("ARCHIVE_DIR", None)
os.environ.pop("USERS_DIR", None)
os.environ.pop("CRAWL_DIR", None)
os.environ.pop("SNAP_DIR", None)


def pytest_configure():
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()
