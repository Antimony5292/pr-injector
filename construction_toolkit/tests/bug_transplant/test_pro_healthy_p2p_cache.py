from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.verify_swebench_pro import (  # noqa: E402
    _healthy_p2p_cache_get,
    _healthy_p2p_cache_put,
    _prune_parent_nodeids,
    _target_behavior_family,
    _target_budget_allows,
)


def test_healthy_p2p_cache_round_trip(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "healthy.sqlite3"
    monkeypatch.setenv("PRI_HEALTHY_P2P_CACHE", str(cache))

    assert _healthy_p2p_cache_get("repo:head:python::test_a") is None
    _healthy_p2p_cache_put("repo:head:python::test_a", True)
    _healthy_p2p_cache_put("repo:head:python::test_b", False)

    assert _healthy_p2p_cache_get("repo:head:python::test_a") is True
    assert _healthy_p2p_cache_get("repo:head:python::test_b") is False


def test_prune_parent_nodeids_keeps_only_specific_independent_targets() -> None:
    tests = [
        "tests/test_a.py",
        "tests/test_a.py::TestA",
        "tests/test_a.py::TestA::test_one",
        "tests/test_a.py::test_two",
        "tests/test_b.py::test_three",
        "tests/test_b.py::test_three",
    ]

    assert _prune_parent_nodeids(tests) == [
        "tests/test_a.py::TestA::test_one",
        "tests/test_a.py::test_two",
        "tests/test_b.py::test_three",
    ]


def test_target_budget_counts_parameter_variants_as_one_behavior() -> None:
    variants = [f"tests/test_a.py::test_value[{i}]" for i in range(14)]
    assert {_target_behavior_family(test) for test in variants} == {
        "tests/test_a.py::test_value"
    }
    assert _target_budget_allows(variants, 6)


def test_target_budget_rejects_too_many_distinct_behaviors() -> None:
    tests = [f"tests/test_a.py::test_{i}" for i in range(7)]
    assert not _target_budget_allows(tests, 6)
