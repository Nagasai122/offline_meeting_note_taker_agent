from __future__ import annotations

import threading
import time

import pytest

from concurrency.lock import FileLock, LockTimeoutError


def test_acquire_and_release_roundtrip(tmp_path):
    # The lock *file* now persists across acquire/release cycles under the
    # portalocker-backed implementation -- it's a lock target, not lock state
    # (the OS-level lock on the open handle is what conveys exclusivity, and
    # that's what's released, not the file's existence). See concurrency/lock.py's
    # module docstring for why this is a deliberate improvement over the
    # previous create-then-delete-file behaviour this test used to assert.
    lock = FileLock(tmp_path / "x.lock", timeout_seconds=1.0)
    lock.acquire()
    assert (tmp_path / "x.lock").exists()
    lock.release()
    assert (tmp_path / "x.lock").exists()  # persists; just no longer held


def test_context_manager_releases_on_exit(tmp_path):
    lock_path = tmp_path / "x.lock"
    with FileLock(lock_path, timeout_seconds=1.0):
        assert lock_path.exists()
    assert lock_path.exists()  # persists; a second acquire below proves it's released

    # Prove release actually happened (not just that the file wasn't deleted):
    # a fresh acquire against the same path must succeed immediately.
    second = FileLock(lock_path, timeout_seconds=0.5)
    second.acquire()
    second.release()


def test_crashed_holder_does_not_permanently_block_a_new_acquire(tmp_path):
    """The correctness property Fix D exists for: a process that dies without
    calling release() (a crash, kill -9, unclean shutdown) must not leave the
    lock stuck forever. Simulated here by closing the file handle directly
    (bypassing FileLock.release()'s portalocker.unlock() call) -- the closest
    a single-process test can get to "the OS reclaims the handle on process
    exit" without literally spawning and killing a subprocess. No PID file,
    no staleness heuristic, no unlink race: the OS-level lock on that handle
    is simply gone once the handle is gone."""
    lock_path = tmp_path / "x.lock"
    crashed = FileLock(lock_path, timeout_seconds=1.0)
    crashed.acquire()
    crashed._fh.close()  # simulate an ungraceful death -- no release(), no unlock()

    contender = FileLock(lock_path, timeout_seconds=2.0, poll_interval_seconds=0.05)
    contender.acquire()  # must not raise LockTimeoutError
    contender.release()


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
