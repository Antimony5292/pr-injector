from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.inject_swebench_pro import _extract_diff  # noqa: E402


def test_fenced_diff_trims_explanatory_text_inside_block() -> None:
    response = """```diff
diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,2 +1,2 @@
-old = True
+old = False
Actually, the important part is that this remains multi-hunk.
```
"""

    diff = _extract_diff(response)

    assert diff is not None
    assert "Actually" not in diff
    assert "+old = False" in diff


def test_bare_diff_trims_trailing_explanation() -> None:
    response = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,2 +1,2 @@
-old = True
+old = False

This is why the patch works.
"""

    diff = _extract_diff(response)

    assert diff is not None
    assert "This is why" not in diff
    assert diff.startswith("diff --git")
