"""Tests for conservative hunk-level reverse surgery."""

from __future__ import annotations

from pr_injector.ast_engine.hunk_surgeon import reverse_patch_hunks_for_file


def test_reverse_patch_hunks_replaces_only_fixed_block():
    patch = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,2 +1,2 @@
 def calc(value, *, modern=True):
-    return value
+    return value + 1
"""
    current = """def calc(value, *, modern=True):
    log(value)
    return value + 1
"""

    result = reverse_patch_hunks_for_file("pkg/mod.py", current, patch)

    assert result.changed is True
    assert "def calc(value, *, modern=True):" in result.content
    assert "    log(value)" in result.content
    assert "    return value\n" in result.content
    assert result.replacements[0].strategy == "exact_added_block"


def test_reverse_patch_hunks_reindents_trimmed_match():
    patch = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,2 +1,2 @@
 def calc(value):
-    return value
+    return value + 1
"""
    current = """class Calculator:
    def calc(self, value):
        return value + 1
"""

    result = reverse_patch_hunks_for_file("pkg/mod.py", current, patch)

    assert result.changed is True
    assert "        return value\n" in result.content
    assert result.replacements[0].strategy == "trimmed_added_block"


def test_reverse_patch_hunks_refuses_ambiguous_match():
    patch = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,2 +1,2 @@
 def calc(value):
-    return value
+    return value + 1
"""
    current = """def a(value):
    return value + 1

def b(value):
    return value + 1
"""

    result = reverse_patch_hunks_for_file("pkg/mod.py", current, patch)

    assert result.changed is False
    assert result.content == current


def test_reverse_patch_hunks_splits_independent_edit_groups():
    patch = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,5 +1,5 @@
 def calc(value):
-    first = value
+    first = value + 1
     middle = value * 2
-    second = value
+    second = value + 1
     return first + second
"""
    current = """def calc(value):
    first = value + 1
    middle = value * 2
    second = value + 1
    return first + second
"""

    result = reverse_patch_hunks_for_file("pkg/mod.py", current, patch)

    assert result.changed is True
    assert "    first = value\n" in result.content
    assert "    second = value\n" in result.content
    assert len(result.replacements) == 2


def test_reverse_patch_hunks_skips_import_only_groups():
    patch = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,2 +1,2 @@
-from old.module import Thing
+from new.module import Thing
 # comment
@@ -10,1 +10,1 @@
-value = Thing.old()
+value = Thing.new()
"""
    current = """from new.module import Thing

value = Thing.new()
"""

    result = reverse_patch_hunks_for_file("pkg/mod.py", current, patch)

    assert result.changed is True
    assert "from new.module import Thing" in result.content
    assert "value = Thing.old()" in result.content
    assert len(result.replacements) == 1
