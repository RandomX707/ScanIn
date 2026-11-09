from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from packages.workflow.release_lock import ReleaseInProgress, claim_release


class _LockConnection:
    def __init__(self, lock: threading.Lock):
        self.lock = lock
        self.held = False

    def execute(self, statement, params):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            self.held = self.lock.acquire(blocking=False)
            return _Result(self.held)
        if "pg_advisory_unlock" in sql:
            if self.held:
                self.lock.release()
                self.held = False
            return _Result(True)
        raise AssertionError(sql)

    def commit(self):
        pass

    def close(self):
        pass


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _Engine:
    def __init__(self):
        self.lock = threading.Lock()

    @contextmanager
    def connect(self):
        yield _LockConnection(self.lock)


def test_only_one_request_claims_an_edit_set_release_at_a_time():
    engine = _Engine()
    entered, finish = threading.Event(), threading.Event()

    def first():
        with claim_release(engine, "9f3ea43e-23ac-4cf4-a00a-0c315d77b851"):
            entered.set()
            assert finish.wait(2)

    def second():
        assert entered.wait(2)
        with pytest.raises(ReleaseInProgress):
            with claim_release(engine, "9f3ea43e-23ac-4cf4-a00a-0c315d77b851"):
                pytest.fail("a second caller acquired the active release claim")

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first)
        contender = pool.submit(second)
        contender.result(timeout=3)
        finish.set()
        owner.result(timeout=3)

    with claim_release(engine, "9f3ea43e-23ac-4cf4-a00a-0c315d77b851"):
        pass


def test_twenty_concurrent_claim_races_each_have_one_owner():
    for iteration in range(20):
        engine = _Engine()
        edit_set_id = f"synthetic-edit-set-{iteration}"
        entered = threading.Event()
        release = threading.Event()
        loser = threading.Event()
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait(timeout=2)
            try:
                with claim_release(engine, edit_set_id):
                    entered.set()
                    assert release.wait(2)
                    return "owner"
            except ReleaseInProgress:
                loser.set()
                return "contender"

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(attempt) for _ in range(2)]
            assert entered.wait(2)
            assert loser.wait(2)
            release.set()
            assert sorted(future.result(timeout=3) for future in attempts) == ["contender", "owner"]
