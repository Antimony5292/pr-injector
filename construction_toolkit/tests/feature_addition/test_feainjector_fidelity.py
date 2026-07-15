from construction_toolkit.feature_addition.scripts.feainjector_fidelity import feature_fidelity, implementation_diff


def test_implementation_diff_excludes_docs_and_tests() -> None:
    patch = """diff --git a/docs/feature.rst b/docs/feature.rst
--- a/docs/feature.rst
+++ b/docs/feature.rst
@@ -1 +1,2 @@
+docs
diff --git a/pkg/feature.py b/pkg/feature.py
--- a/pkg/feature.py
+++ b/pkg/feature.py
@@ -1 +1,2 @@
+def feature():
+    return True
diff --git a/tests/test_feature.py b/tests/test_feature.py
--- a/tests/test_feature.py
+++ b/tests/test_feature.py
@@ -1 +1,2 @@
+def test_feature():
+    assert feature()
"""

    filtered = implementation_diff(patch)

    assert "pkg/feature.py" in filtered
    assert "docs/feature.rst" not in filtered
    assert "tests/test_feature.py" not in filtered


def test_feature_fidelity_rejects_local_complexity_collapse() -> None:
    source = """diff --git a/pkg/feature.py b/pkg/feature.py
--- a/pkg/feature.py
+++ b/pkg/feature.py
@@ -1 +1,7 @@
+def feature():
+    prepare()
+    validate()
+    execute()
+    finalize()
+    report()
"""
    modern = """diff --git b/pkg/feature.py a/pkg/feature.py
--- b/pkg/feature.py
+++ a/pkg/feature.py
@@ -1 +1,2 @@
+def feature():
"""

    gate = feature_fidelity(
        source,
        modern,
        source_target_tests=1,
        modern_target_tests=1,
        source_regression_tests=10,
        modern_regression_tests=10,
    )

    assert gate["passed"] is False
    assert "localized_simplified" in gate["tags"]


def test_fidelity_folds_historical_python_stub_into_modern_typed_module() -> None:
    source = """diff --git a/pkg/api.py b/pkg/api.py
--- a/pkg/api.py
+++ b/pkg/api.py
@@ -1 +1,3 @@
+def feature(match=()):
+    return match
diff --git a/pkg/api.pyi b/pkg/api.pyi
--- a/pkg/api.pyi
+++ b/pkg/api.pyi
@@ -1 +1,3 @@
+def feature(match: tuple = ...) -> tuple: ...
"""
    modern = """diff --git b/pkg/api.py a/pkg/api.py
--- b/pkg/api.py
+++ a/pkg/api.py
@@ -1 +1,4 @@
+def feature(match: tuple = ()) -> tuple:
+    return match
"""

    gate = feature_fidelity(
        source,
        modern,
        source_target_tests=1,
        modern_target_tests=1,
        source_regression_tests=8,
        modern_regression_tests=8,
    )

    assert gate["architecture_normalization"]["source_effective_files"] == 1
    assert gate["architecture_normalization"]["modern_effective_files"] == 1
    assert gate["parts"]["files"] == 1.0
