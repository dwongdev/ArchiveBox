import time

import psutil
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Stop a foreground-owned supervisord if its exact ArchiveBox owner Process exits."

    def add_arguments(self, parser):
        parser.add_argument("--supervisord-process-id", required=True)
        parser.add_argument("--interval", type=float, default=1.0)

    def handle(self, *args, **kwargs):
        from archivebox.core.shutdown_util import wait_psutil_and_kill_children
        from archivebox.machine.models import Process

        supervisord_process_id = kwargs["supervisord_process_id"]
        interval = max(0.2, float(kwargs["interval"]))

        while True:
            try:
                supervisord_process = Process.objects.select_related("parent").get(id=supervisord_process_id)
            except Process.DoesNotExist:
                return

            if supervisord_process.status != Process.StatusChoices.RUNNING:
                return

            supervisord = supervisord_process.proc
            if supervisord is None:
                supervisord_process.mark_exited(exit_code=0)
                return

            owner = supervisord_process.parent
            if owner is not None and owner.is_running:
                time.sleep(interval)
                continue

            try:
                children = supervisord.children(recursive=True)
                supervisord.terminate()
                for child in children:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                wait_psutil_and_kill_children(supervisord, children, timeout=5)
                supervisord_process.mark_exited(exit_code=0)
            except psutil.NoSuchProcess:
                supervisord_process.mark_exited(exit_code=0)
            except (BrokenPipeError, OSError, psutil.TimeoutExpired):
                pass
            return
