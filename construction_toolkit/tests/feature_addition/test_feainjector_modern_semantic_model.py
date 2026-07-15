from construction_toolkit.feature_addition.scripts.construct_feainjector_modern_semantic_model import (
    modern_path_candidates,
    reanchor_unified_diff,
)


def test_modern_path_candidates_resolves_src_layout_drift(tmp_path) -> None:
    modern = tmp_path / "src" / "kinto_http" / "session.py"
    modern.parent.mkdir(parents=True)
    modern.write_text("class Session:\n    pass\n", encoding="utf-8")

    assert modern_path_candidates("kinto_http/session.py", tmp_path) == [
        "src/kinto_http/session.py"
    ]


def test_modern_path_candidates_resolves_private_module_rename(tmp_path) -> None:
    modern = tmp_path / "amaranth" / "hdl" / "_ast.py"
    modern.parent.mkdir(parents=True)
    modern.write_text("class Value:\n    pass\n", encoding="utf-8")

    assert modern_path_candidates("amaranth/hdl/ast.py", tmp_path) == [
        "amaranth/hdl/_ast.py"
    ]


def test_reanchor_unified_diff_uses_exact_modern_source_location(tmp_path) -> None:
    source = tmp_path / "pkg" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join([*(f"line_{i}" for i in range(20)), "def target():", "    return 1"]) + "\n")
    stale = """diff --git a/pkg/module.py b/pkg/module.py
--- a/pkg/module.py
+++ b/pkg/module.py
@@ -2,99 +2,99 @@
 def target():
-    return 1
+    return 2
"""

    reanchored = reanchor_unified_diff(stale, tmp_path)

    assert "@@ -21,2 +21,2 @@" in reanchored
