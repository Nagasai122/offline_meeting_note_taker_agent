from __future__ import annotations

import threading
import time

import pytest

from concurrency.lock import FileLock, LockTimeoutError


def test_acquire_and_release_roundtrip(tmp_path):
    lock = FileLock(tmp_path / "x.lock", timeout_seconds=1.0)
    lock.acquire()
    assert (tmp_path / "x.lock").exists()
    lock.release()
    assert not (tmp_path / "x.lock").exists()


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / "x.lock"
    with FileLock(lock_path, timeout_seconds=1.0):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_second_acquire_times_out_while_held(tmp_path):
    lock_path = tmp_path / "x.lock"
    holder = FileLock(lock_path, timeout_seconds=1.0)
    holder.acquire()
    try:
        contender = FileLock(lock_path, timeout_seconds=0.2, poll_interval_seconds=0.02)
        with pytest.raises(LockTimeoutError):
            contender.acquire()
    finally:
        holder.release()


def test_concurrent_threads_serialise_through_the_lock(tmp_path):
    lock_path = tmp_path / "x.lock"
    counter_path = tmp_path / "counter.txt"
    counter_path.write_text("0")
    errors = []

    def bump():
        try:
            with FileLock(lock_path, timeout_seconds=5.0, poll_interval_seconds=0.01):
                value = int(counter_path.read_text())
                time.sleep(0.01)  # widen the window where a race would corrupt this
                counter_path.write_text(str(value + 1))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert int(counter_path.read_text()) == 10
