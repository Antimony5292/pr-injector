from construction_toolkit.feature_addition.scripts.merge_feainjector_strict_cases import source_feature_rejection


def test_source_gate_rejects_explicit_bug_task():
    assert source_feature_rejection({"source_task_type": "bug"}) == "source_explicit_bug_task"


def test_source_gate_rejects_bugfix_changelog_section():
    row = {"feature_patch": "+### Bug fixes\n+Fixed the cache behavior\n"}

    assert source_feature_rejection(row) == "source_bugfix_section"


def test_source_gate_keeps_feature_task():
    row = {
        "source_task_type": "enhancement",
        "feature_patch": "diff --git a/a.py b/a.py\n",
        "feature_gate_pass": True,
    }

    assert source_feature_rejection(row) is None
