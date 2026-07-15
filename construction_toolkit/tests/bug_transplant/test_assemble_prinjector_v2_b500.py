import json

from construction_toolkit.bug_transplant.scripts.assemble_prinjector_v2_b500 import (
    FidelityGateConfig,
    read_injection_index,
    strict_and_v2_ok,
    verification_rows,
)


def write_row(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_discovers_nested_construction_lanes(tmp_path):
    shard = tmp_path / "force_l3" / "shard_new_l1l2_001_resume"
    write_row(
        shard / "verified_injection_results.jsonl",
        {"instance_id": "owner__repo-1", "success": True},
    )
    write_row(
        shard / "verified_verification_results.jsonl",
        {"instance_id": "owner__repo-1", "verification": {"status": "completed"}},
    )

    injections = read_injection_index([tmp_path])
    verifications = verification_rows([tmp_path])

    assert set(injections) == {"owner__repo-1"}
    assert [row["instance_id"] for row in verifications] == ["owner__repo-1"]
    assert injections["owner__repo-1"]["construction_injection_source"].endswith(
        "verified_injection_results.jsonl"
    )
    assert verifications[0]["construction_verification_source"].endswith(
        "verified_verification_results.jsonl"
    )


def test_successful_retry_is_not_overwritten_by_later_stale_failure(tmp_path):
    successful = tmp_path / "a_manual" / "shard_new_l1l2_001_manual"
    stale_failure = tmp_path / "z_old" / "shard_new_l1l2_001_old"
    write_row(
        successful / "verified_injection_results.jsonl",
        {
            "instance_id": "owner__repo-1",
            "success": True,
            "v2_fidelity_gate_pass": True,
            "v2_fidelity_gate": {"pass_gate": True, "score": 0.8},
        },
    )
    write_row(
        stale_failure / "verified_injection_results.jsonl",
        {"instance_id": "owner__repo-1", "success": False},
    )

    injections = read_injection_index([tmp_path])

    assert injections["owner__repo-1"]["success"] is True
    assert "a_manual" in injections["owner__repo-1"]["construction_injection_source"]


def test_strict_gate_rejects_collapsed_target_surface():
    ok, reason, audit = strict_and_v2_ok(
        {
            "verification": {
                "pass_to_fail": True,
                "target_retention_ratio": 0.25,
                "verified_target_count": 1,
                "collectable_target_count_before_healthy_filter": 4,
            }
        },
        {"success": True},
        {},
        FidelityGateConfig(),
    )

    assert ok is False
    assert reason == "target_surface_collapse"
    assert audit["target_retention_ratio"] == 0.25
