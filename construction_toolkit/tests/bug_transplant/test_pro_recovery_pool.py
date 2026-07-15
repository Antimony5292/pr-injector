from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.build_prinjector_v2_recovery_pool import (  # noqa: E402
    classify_failure,
    classify_strict_verification,
)


def test_strict_p2f_miss_is_recoverable_with_behavior_feedback() -> None:
    reason, feedback = classify_strict_verification(
        {"verification": {"pass_to_fail": False}}
    ) or (None, None)
    assert reason == "strict_p2f_miss"
    assert "modern call chain" in feedback


def test_strict_p2p_failure_includes_broken_adjacent_tests() -> None:
    reason, feedback = classify_strict_verification({
        "verification": {
            "pass_to_fail": True,
            "golden_repair_pass": True,
            "p2p_buggy_failed": 1,
            "p2p_buggy_failed_tests": ["tests/test_neighbor.py::test_safe"],
            "p2p_repaired_pass": True,
        }
    }) or (None, None)
    assert reason == "strict_p2p_buggy_regression"
    assert "test_neighbor.py" in feedback


def test_full_strict_pass_is_not_retried() -> None:
    assert classify_strict_verification({
        "verification": {
            "pass_to_fail": True,
            "golden_repair_pass": True,
            "p2p_buggy_failed": 0,
            "p2p_repaired_pass": True,
        }
    }) is None


def test_overlapping_target_selector_skip_is_recoverable() -> None:
    reason, feedback = classify_strict_verification({
        "verification": {
            "status": "skipped",
            "reason": "too_many_target_tests",
        }
    }) or (None, None)
    assert reason == "strict_target_selector_normalization_retry"
    assert "most specific leaf nodeids" in feedback


def test_codex_usage_window_is_recoverable() -> None:
    assert classify_failure(
        {"failure_reason": "You've hit your usage limit. try again at 4:21 AM."},
        include_l3_gate_fail=True,
    ) == "codex_usage_window_retry"


def test_known_healthy_environment_failures_are_recoverable() -> None:
    assert classify_failure(
        {
            "failure_reason": "preflight_failed: healthy_target_failed",
            "preflight": {
                "healthy_result": {
                    "output_tail": "ModuleNotFoundError: No module named 'luqum'"
                }
            },
        },
        include_l3_gate_fail=True,
    ) == "openlibrary_runtime_dependency_fixed"
    assert classify_failure(
        {
            "failure_reason": "preflight_failed: healthy_target_failed",
            "preflight": {
                "healthy_result": {
                    "output_tail": "QtWarningMsg: Populating font family aliases took 71 ms"
                }
            },
        },
        include_l3_gate_fail=True,
    ) == "qutebrowser_headless_qt_policy_fixed"
