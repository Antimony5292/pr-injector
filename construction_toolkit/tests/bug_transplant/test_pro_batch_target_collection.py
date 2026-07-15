from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.verify_swebench_pro import _collectable_tests  # noqa: E402


def test_parameterized_targets_use_one_file_collection(tmp_path, monkeypatch) -> None:
    test_file = tmp_path / "tests/test_values.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_value(): pass\n", encoding="utf-8")
    calls = []

    def fake_collect(worktree, path, python, timeout=60):
        calls.append(path)
        return [
            "tests/test_values.py::test_value[one]",
            "tests/test_values.py::test_value[two]",
        ]

    monkeypatch.setattr(
        "construction_toolkit.bug_transplant.scripts.verify_swebench_pro._pytest_collect_nodeids", fake_collect
    )
    tests = [
        "tests/test_values.py::test_value[old-one]",
        "tests/test_values.py::test_value[old-two]",
        "tests/test_values.py::test_value[old-three]",
        "tests/test_values.py::test_value[old-four]",
    ]

    assert _collectable_tests(str(tmp_path), "repo/name", tests, "python") == [
        "tests/test_values.py::test_value[one]",
        "tests/test_values.py::test_value[two]",
    ]
    assert calls == ["tests/test_values.py"]
