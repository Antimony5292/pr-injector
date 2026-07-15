from pathlib import Path

from construction_toolkit.feature_addition.scripts.build_feature_external_source_manifest import normalize_row
from construction_toolkit.feature_addition.scripts.construct_feainjector_modern_semantic_model import missing_python_nodeids
from construction_toolkit.feature_addition.scripts.verify_feainjector_feature_tasks import normalize_relative_nodeids


def test_bugfix_changelog_is_not_accepted_as_feature() -> None:
    row = normalize_row(
        "example/bench",
        "test",
        0,
        {
            "instance_id": "owner__repo-1",
            "repo": "owner/repo",
            "problem_statement": "Fix incorrect password handling and preserve existing behavior.",
            "patch": """diff --git a/changelog.md b/changelog.md
--- a/changelog.md
+++ b/changelog.md
@@ -1 +1,4 @@
+### Bug fixes
+
+- Fix password handling
diff --git a/pkg/api.py b/pkg/api.py
--- a/pkg/api.py
+++ b/pkg/api.py
@@ -1,6 +1,7 @@
 def existing_api(value):
+    value = normalize(value)
     return value
""",
            "FAIL_TO_PASS": ["tests/test_api.py::test_password"],
            "language": "python",
        },
    )

    assert row["feature_gate_pass"] is False
    assert "bugfix_task_misclassified_as_feature" in row["feature_gate_reasons"]


def test_missing_python_nodeids_checks_class_and_method(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_api.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "class TestAPI:\n"
        "    def test_current(self):\n"
        "        pass\n\n"
        "def test_top_level():\n"
        "    pass\n",
        encoding="utf-8",
    )

    nodeids = [
        "tests/test_api.py::TestAPI::test_current[param]",
        "tests/test_api.py::TestAPI::test_removed",
        "tests/test_api.py::test_top_level",
        "tests/missing.py::test_absent",
    ]

    assert missing_python_nodeids(tmp_path, nodeids) == [
        "tests/test_api.py::TestAPI::test_removed",
        "tests/missing.py::test_absent",
    ]


def test_relative_p2p_nodeids_are_bound_to_feature_test_module() -> None:
    feature = ["tests/unit/test_reddit.py::TestReddit::test_target"]
    p2p = [
        "::TestReddit::test_neighbor",
        "tests/unit/test_reddit.py::TestReddit::test_explicit",
        "::TestReddit::test_after_explicit",
    ]

    assert normalize_relative_nodeids(p2p, feature + p2p) == [
        "tests/unit/test_reddit.py::TestReddit::test_neighbor",
        "tests/unit/test_reddit.py::TestReddit::test_explicit",
        "tests/unit/test_reddit.py::TestReddit::test_after_explicit",
    ]
