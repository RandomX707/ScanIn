from argparse import Namespace
import json

import pytest

from packages.core.planning.planner import PlanError
from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.planning.planner import build_patch_plan
from scripts.m0_xlsx_demo import apply_cmd, plan_dict
from tests.conftest import add_shared_strings, add_unsupported_cells, make_fixture


def test_cli_refuses_to_write_candidate_over_original_path(tmp_path):
    source = make_fixture(tmp_path / "original.xlsx")
    add_shared_strings(source)
    add_unsupported_cells(source)
    before = source.read_bytes()
    with pytest.raises(PlanError, match="output_must_be_new_copy"):
        apply_cmd(Namespace(source=str(source), output=str(source), plan=str(tmp_path / "missing-plan.json")))
    assert source.read_bytes() == before


def test_cli_noop_does_not_create_a_candidate_file(tmp_path, capsys):
    source = make_fixture(tmp_path / "original.xlsx")
    add_shared_strings(source)
    add_unsupported_cells(source)
    source_bytes = source.read_bytes()
    plan = build_patch_plan(XlsxAdapter(), source_bytes, [], provenance={"edit_set_id": "empty"})
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_dict(plan)), encoding="utf-8")
    candidate = tmp_path / "derived.xlsx"
    apply_cmd(Namespace(source=str(source), output=str(candidate), plan=str(plan_path)))
    assert not candidate.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "NO_CHANGE"
