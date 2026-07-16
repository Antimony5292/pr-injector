import os
from pathlib import Path

from construction_toolkit.bug_transplant.scripts.inject_swebench_pro import coerce_list
from construction_toolkit.bug_transplant.scripts.verify_swebench_pro import (
    _coerce_list,
    _patchlevel_runtime_compatible,
    _pytest_config_args,
    _worktree_relative_nodeid,
)


def test_python_literal_test_lists_are_not_collapsed() -> None:
    value = "['tests/test_api.py::test_one', 'tests/test_api.py::test_two']"

    expected = ["tests/test_api.py::test_one", "tests/test_api.py::test_two"]
    assert coerce_list(value) == expected
    assert _coerce_list(value) == expected


def test_double_encoded_python_literal_test_lists_are_not_collapsed() -> None:
    value = '"[\'tests/test_api.py::test_one\', \'tests/test_api.py::test_two\']"'

    expected = ["tests/test_api.py::test_one", "tests/test_api.py::test_two"]
    assert coerce_list(value) == expected
    assert _coerce_list(value) == expected


def test_list_wrapped_python_literal_test_lists_are_not_collapsed() -> None:
    value = ["['tests/test_api.py::test_one', 'tests/test_api.py::test_two']"]

    expected = ["tests/test_api.py::test_one", "tests/test_api.py::test_two"]
    assert coerce_list(value) == expected
    assert _coerce_list(value) == expected


def test_malformed_scalar_remains_a_single_test_identifier() -> None:
    value = "tests/test_api.py::test_one"

    assert coerce_list(value) == [value]
    assert _coerce_list(value) == [value]


def test_collected_nodeid_is_made_relative_to_worktree(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_api.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_one(): pass\n", encoding="utf-8")

    absolute = f"{test_file}::test_one"
    assert _worktree_relative_nodeid(tmp_path, absolute) == "tests/test_api.py::test_one"


def test_collected_nodeid_strips_non_absolute_root_prefix(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_api.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_one(): pass\n", encoding="utf-8")

    prefixed = ".pri-workspace/run/worktree/tests/test_api.py::test_one"
    assert _worktree_relative_nodeid(tmp_path, prefixed) == "tests/test_api.py::test_one"


def test_invalid_scalar_pytest_testpaths_uses_explicit_config_override(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = "tests"\n', encoding="utf-8"
    )

    assert _pytest_config_args(tmp_path) == ["-c", os.devnull]


def test_patchlevel_runtime_tolerance_is_narrow() -> None:
    assert _patchlevel_runtime_compatible((3, 14, 6), ">=3.14.5,<3.14.6")
    assert not _patchlevel_runtime_compatible((3, 13, 9), ">=3.14.5,<3.14.6")
    assert not _patchlevel_runtime_compatible((3, 14, 8), ">=3.14.5,<3.14.6")
