from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.inject_swebench_pro import (  # noqa: E402
    _target_preflight_cache_get,
    _target_preflight_cache_put,
)


def test_target_preflight_cache_stores_only_success(tmp_path) -> None:
    path = tmp_path / "target.json"
    _target_preflight_cache_put(path, {"ok": False, "reason": "failed"})
    assert not path.exists()

    _target_preflight_cache_put(
        path,
        {
            "ok": True,
            "collectable_target_tests": ["tests/test_a.py::test_target"],
        },
    )
    cached = _target_preflight_cache_get(path)
    assert cached is not None
    assert cached["ok"] is True
    assert cached["cache_hit"] is True
