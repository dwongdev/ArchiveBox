import os
import signal
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.django_db


def test_runtime_stack_owner_prefers_newer_server_over_older_update(tmp_path):
    from archivebox.machine.models import Machine, Process
    from archivebox.services.supervision_service import runtime_stack_owner

    procs: list[subprocess.Popen[str]] = []
    try:
        for process_type in (Process.TypeChoices.UPDATE, Process.TypeChoices.SERVER):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            procs.append(proc)
            Process.objects.create(
                machine=Machine.current(),
                process_type=process_type,
                worker_type=process_type,
                pwd=str(tmp_path),
                cmd=[],
                pid=proc.pid,
                status=Process.StatusChoices.RUNNING,
            )
            time.sleep(0.05)

        owner = runtime_stack_owner(data_dir=tmp_path)

        assert owner is not None
        assert owner.process_type == Process.TypeChoices.SERVER
        assert owner.pid == procs[-1].pid
    finally:
        for proc in procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)


def test_runtime_stack_owner_keeps_server_over_newer_supervised_runner(tmp_path):
    from archivebox.machine.models import Machine, Process
    from archivebox.services.supervision_service import RUNNER_ACTIVE_WORKER_TYPE, runtime_stack_owner

    procs: list[subprocess.Popen[str]] = []
    try:
        for process_type, worker_type in (
            (Process.TypeChoices.SERVER, ""),
            (Process.TypeChoices.ORCHESTRATOR, RUNNER_ACTIVE_WORKER_TYPE),
        ):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            procs.append(proc)
            Process.objects.create(
                machine=Machine.current(),
                process_type=process_type,
                worker_type=worker_type,
                pwd=str(tmp_path),
                cmd=[],
                pid=proc.pid,
                status=Process.StatusChoices.RUNNING,
            )
            time.sleep(0.05)

        owner = runtime_stack_owner(data_dir=tmp_path)

        assert owner is not None
        assert owner.process_type == Process.TypeChoices.SERVER
        assert owner.pid == procs[0].pid
    finally:
        for proc in procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)


def test_runtime_stack_owner_reaps_dead_newer_parent_and_promotes_next_live_command(tmp_path):
    from archivebox.machine.models import Machine, Process
    from archivebox.services.supervision_service import runtime_stack_owner

    procs: list[subprocess.Popen[str]] = []
    older_row = None
    newer_row = None
    try:
        for process_type in (Process.TypeChoices.UPDATE, Process.TypeChoices.SERVER):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            procs.append(proc)
            row = Process.objects.create(
                machine=Machine.current(),
                process_type=process_type,
                worker_type=process_type,
                pwd=str(tmp_path),
                cmd=[],
                pid=proc.pid,
                status=Process.StatusChoices.RUNNING,
            )
            if process_type == Process.TypeChoices.UPDATE:
                older_row = row
            else:
                newer_row = row
            time.sleep(0.05)

        assert older_row is not None
        assert newer_row is not None

        os.killpg(procs[-1].pid, signal.SIGTERM)
        procs[-1].wait(timeout=5)

        owner = runtime_stack_owner(data_dir=tmp_path)

        assert owner is not None
        assert owner.id == older_row.id
        assert owner.pid == procs[0].pid
        newer_row.refresh_from_db()
        assert newer_row.status == Process.StatusChoices.EXITED
    finally:
        for proc in procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)


def test_runtime_stack_owner_ignores_supervised_orphan_runner(tmp_path):
    from archivebox.machine.models import Machine, Process
    from archivebox.services.supervision_service import RUNNER_ACTIVE_WORKER_TYPE, runtime_stack_owner

    procs: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            procs.append(proc)

        supervisor_row = Process.objects.create(
            machine=Machine.current(),
            process_type=Process.TypeChoices.SUPERVISORD,
            worker_type="supervisord",
            pwd=str(tmp_path),
            cmd=[],
            pid=procs[0].pid,
            status=Process.StatusChoices.RUNNING,
        )
        Process.objects.create(
            machine=Machine.current(),
            parent=supervisor_row,
            process_type=Process.TypeChoices.ORCHESTRATOR,
            worker_type=RUNNER_ACTIVE_WORKER_TYPE,
            pwd=str(tmp_path),
            cmd=[],
            pid=procs[1].pid,
            status=Process.StatusChoices.RUNNING,
        )

        assert runtime_stack_owner(data_dir=tmp_path) is None
    finally:
        for proc in procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)


def test_runtime_stack_owner_allows_top_level_runner_when_no_parent_command_exists(tmp_path):
    from archivebox.machine.models import Machine, Process
    from archivebox.services.supervision_service import RUNNER_ACTIVE_WORKER_TYPE, runtime_stack_owner

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        runner_row = Process.objects.create(
            machine=Machine.current(),
            process_type=Process.TypeChoices.ORCHESTRATOR,
            worker_type=RUNNER_ACTIVE_WORKER_TYPE,
            pwd=str(tmp_path),
            cmd=[],
            pid=proc.pid,
            status=Process.StatusChoices.RUNNING,
        )

        owner = runtime_stack_owner(data_dir=tmp_path)

        assert owner is not None
        assert owner.id == runner_row.id
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
