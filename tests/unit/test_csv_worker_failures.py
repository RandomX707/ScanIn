"""Phase 2B: worker resource failures and corrupted worker output always fail closed."""
from __future__ import annotations

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from packages.workers.isolation import RestrictedSubprocess, RestrictedWorkerFailed
from packages.workers.parser import ParserNotVerified, RestrictedParser


def test_worker_exceeding_its_time_budget_is_terminated(tmp_path):
    runner = RestrictedSubprocess(project_root=tmp_path, platform_name="darwin")
    with pytest.raises(RestrictedWorkerFailed, match="worker_timeout"):
        runner._run_bounded([sys.executable, "-c", "import time; time.sleep(5)"], {}, timeout_seconds=1)


class _Runner:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def run(self, command, **kwargs):
        return self.behaviour(command)


@pytest.mark.parametrize("behaviour", [
    lambda command: (_ for _ in ()).throw(RestrictedWorkerFailed("worker_timeout")),
    lambda command: (_ for _ in ()).throw(RestrictedWorkerFailed("worker_memory_limit_exceeded")),
    lambda command: SimpleNamespace(returncode=-9, stdout="", stderr=""),
    lambda command: SimpleNamespace(returncode=0, stdout='{"status":"inspected","sha256"', stderr=""),
    lambda command: SimpleNamespace(returncode=0, stdout=json.dumps({"status": "rejected", "reason": "parser_input_unavailable"}), stderr=""),
    lambda command: SimpleNamespace(returncode=0, stdout=json.dumps({
        "status": "inspected", "sha256": "0" * 64, "size_bytes": 9, "media_type": "text/csv",
        "capability": {"level": "editable", "reasons": []}, "adapter": "csv"}), stderr=""),
], ids=["timeout", "memory", "killed", "truncated_output", "input_unavailable", "source_hash_mismatch"])
def test_restricted_parser_turns_worker_failures_into_not_verified(tmp_path, behaviour):
    source = tmp_path / "large.csv"
    source.write_bytes(b"a,b\n1,2\n")
    parser = RestrictedParser(tmp_path, runner=_Runner(behaviour))
    with pytest.raises(ParserNotVerified):
        parser.inspect(source, include_targets=True, target_ids=["csv:60000:3"])


def test_restricted_parser_accepts_only_a_matching_complete_report(tmp_path):
    source = tmp_path / "ok.csv"
    data = b"a,b\n1,2\n"
    source.write_bytes(data)
    report = {"status": "inspected", "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
              "media_type": "text/csv", "capability": {"level": "editable", "reasons": []}, "adapter": "csv",
              "targets": [], "targets_page": {"mode": "page", "offset": 0, "limit": 2000, "total": 4, "next_offset": None}}
    parser = RestrictedParser(tmp_path, runner=_Runner(lambda command: SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")))
    assert parser.inspect(source, include_targets=True).targets_page["total"] == 4
