"""Regression: the comparison writer and the cell-marks reader must share ONE cache (Starlette State keeps attributes in `_state`)."""
from types import SimpleNamespace

from starlette.datastructures import State

from packages.api.compare import _compare_cache


def test_writer_and_reader_see_the_same_cache_object():
    request = SimpleNamespace(app=SimpleNamespace(state=State()))
    writer = _compare_cache(request)
    writer[("tenant", "a", "b")] = {"changes": []}
    assert _compare_cache(request) is writer and ("tenant", "a", "b") in _compare_cache(request)


def test_an_existing_plain_dict_is_adopted_not_lost():
    state = State(); state.compare_results = {("t", "a", "b"): {"changes": [1]}}
    cache = _compare_cache(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert ("t", "a", "b") in cache and state.compare_results is cache
