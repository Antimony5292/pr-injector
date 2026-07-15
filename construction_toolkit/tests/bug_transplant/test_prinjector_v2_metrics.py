import gzip
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.prinjector_v2_metrics import (  # noqa: E402
    evaluate_patch_pair_fidelity,
    read_jsonl,
)


PATCH = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,5 +1,5 @@
-def first():
-    return "old"
+def first():
+    return "new"
@@ -10,5 +10,5 @@
-def second():
-    return "old"
+def second():
+    return "new"
"""


SIMPLIFIED = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
"""


def test_identical_patch_pair_passes_gate() -> None:
    gate = evaluate_patch_pair_fidelity(
        a_patch=PATCH,
        b_patch=PATCH,
        a_fail_to_pass=["test_target"],
        b_fail_to_pass=["test_target"],
        a_pass_to_pass=["test_adjacent_1", "test_adjacent_2"],
        b_pass_to_pass=["test_adjacent_1", "test_adjacent_2"],
        injection_level="Level_1_Clean_Revert",
    )

    assert gate["pass_gate"] is True
    assert gate["score"] == 1.0


def test_simplified_patch_pair_fails_gate() -> None:
    gate = evaluate_patch_pair_fidelity(
        a_patch=PATCH,
        b_patch=SIMPLIFIED,
        a_fail_to_pass=["test_target"],
        b_fail_to_pass=["test_target"],
        a_pass_to_pass=["test_adjacent_1", "test_adjacent_2"],
        b_pass_to_pass=["test_adjacent_1", "test_adjacent_2"],
        injection_level="Level_2_AST_Surgery",
    )

    assert gate["pass_gate"] is False
    assert "localized_simplified" in gate["tags"]


def test_read_jsonl_supports_gzip(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl.gz"
    expected = [{"instance_id": "one"}, {"instance_id": "two"}]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in expected:
            handle.write(json.dumps(row) + "\n")

    assert read_jsonl(path) == expected
