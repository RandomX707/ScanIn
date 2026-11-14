from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_xlsx_benchmark_ignores_formula_protected_and_empty_cells():
    benchmark = _load("xlsx_benchmark", "scripts/corpus/xlsx_benchmark.py")
    window = {"cells": [
        [1, 1, "=A1+1", "string", None, 0, "formula_cell"],
        [1, 2, "locked", "string", None, 0, "protected_cell_locked"],
        [1, 3, "", "string", None, 1, None],
        [2, 2, "A long editable label whose length exceeds forty characters", "string", None, 1, None],
    ]}

    selected = benchmark.editable_text_cells(window)

    assert [cell[2] for cell in selected] == ["A long editable label whose length exceeds forty characters"]


def test_acceptance_zip_skip_is_explicit_and_not_a_pass():
    runner = _load("acceptance_run", "scripts/corpus/acceptance_run.py")

    result = runner.skipped_zip_result()

    assert result.verdict == "SKIPPED — OUT OF SCOPE"
    assert result.checks == []


def test_storage_reader_rejects_shared_database():
    reader = _load("storage_read", "scripts/corpus/storage_read.py")

    with pytest.raises(RuntimeError, match="channel360_iso_*"):
        reader.database_name("channel360_test")

    assert "channel360_iso_harden" in reader.database_url("channel360_iso_harden")


def test_native_xlsx_blank_value_accepts_excel_numeric_coercion_but_text_stays_exact():
    runner = _load("acceptance_run_values", "scripts/corpus/acceptance_run.py")

    assert runner.native_cell_value_matches("null", 12345, "12345")
    assert not runner.native_cell_value_matches("string", 12345, "12345")


def test_missing_private_manifest_is_a_hard_failure_not_a_pass(tmp_path):
    runner = _load("acceptance_run_prereq", "scripts/corpus/acceptance_run.py")
    runner.MANIFEST_PATH = tmp_path / "absent-manifest.json"

    with pytest.raises(SystemExit, match="PREREQUISITE MISSING"):
        runner.load_manifest()


def test_benchmark_without_private_workbooks_fails_loudly(tmp_path, monkeypatch):
    benchmark = _load("xlsx_benchmark_prereq", "scripts/corpus/xlsx_benchmark.py")
    monkeypatch.setattr(benchmark, "CORPUS", tmp_path)

    with pytest.raises(SystemExit, match="PREREQUISITE MISSING"):
        benchmark.cases()
