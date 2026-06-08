from django.utils import timezone

from archivebox.workers.models import RETRY_AT_MAX


def test_retry_at_max_is_safe_for_admin_timezone_localization():
    with timezone.override("Pacific/Kiritimati"):
        assert timezone.localtime(RETRY_AT_MAX).year == 9999


# test_crawl_pause_resume_api_survives_server_restart_and_processes_after_resume moved to test_api_v1_crawls_crawl_crawl_id.py.
# test_update_index_only_runs_paused_search_rows_and_resume_later_runs_crawl moved to test_api_v1_crawls_crawl_crawl_id.py.
