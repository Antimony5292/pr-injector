from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.reverify_pro_harness_false_rejects import (  # noqa: E402
    harness_false_reject_reason,
)


def test_official_target_is_not_an_adjacent_regression() -> None:
    row = {
        "verification": {
            "pass_to_fail": True,
            "p2p_buggy_failed_tests": ["tests/test_a.py::test_feature[2]"],
        }
    }
    injection = {"fail_to_pass": ["tests/test_a.py::test_feature[1]"]}
    candidate = {
        "fail_to_pass": [
            "tests/test_a.py::test_feature[1]",
            "tests/test_a.py::test_feature[2]",
        ]
    }
    assert harness_false_reject_reason(row, injection, candidate) == (
        "official_targets_mislabeled_as_adjacent_p2p"
    )


def test_real_adjacent_regression_is_not_harness_salvaged() -> None:
    row = {
        "verification": {
            "pass_to_fail": True,
            "p2p_buggy_failed_tests": ["tests/test_neighbor.py::test_safe"],
        }
    }
    injection = {"fail_to_pass": ["tests/test_a.py::test_feature"]}
    candidate = {"fail_to_pass": ["tests/test_a.py::test_feature"]}
    assert harness_false_reject_reason(row, injection, candidate) is None
