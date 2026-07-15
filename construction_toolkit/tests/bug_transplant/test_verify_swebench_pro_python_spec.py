from construction_toolkit.bug_transplant.scripts.verify_swebench_pro import _editable_install_args, _version_satisfies


def test_two_segment_compatible_release_allows_later_minor_versions():
    assert _version_satisfies((3, 12, 13), "~=3.9")
    assert not _version_satisfies((4, 0, 0), "~=3.9")


def test_three_segment_compatible_release_stays_within_minor_version():
    assert _version_satisfies((3, 9, 8), "~=3.9.1")
    assert not _version_satisfies((3, 10, 0), "~=3.9.1")


def test_compiled_repos_disable_build_isolation_for_editable_install():
    assert _editable_install_args("scikit-learn/scikit-learn", ".[test]") == [
        "--no-build-isolation",
        "-e",
        ".[test]",
    ]
    assert _editable_install_args("matplotlib/matplotlib", ".") == [
        "--no-build-isolation",
        "-e",
        ".",
    ]


def test_pure_python_repo_keeps_default_editable_install():
    assert _editable_install_args("psf/requests", ".[test]") == ["-e", ".[test]"]
