"""planner 纯逻辑测试:最小覆盖、共享文件保留、可释放候选、规则、指纹。"""
from app.planner import (canonical_digest, compute_plan, minimal_cover,
                         registry_fingerprint)


def snap(sid, parts, status="COMPLETE", created="2026-11-01T00:00:00Z", files=None):
    if files is None:
        files = [
            {"path": f"data/{sid}/{i}.parquet", "size_bytes": 100 + i, "partitions": [p]}
            for i, p in enumerate(parts)
        ]
    return {"snapshot_id": sid, "status": status, "created_at": created,
            "digest": canonical_digest({"snapshot_id": sid, "files": files}),
            "uploaded_at": created, "files": files}


def lease(lid, parts, version=1):
    return {"lease_id": lid, "name": lid, "required_partitions": parts,
            "version": version, "created_at": "", "updated_at": ""}


RULES0 = {"min_snapshots": 0, "protect_days": 0}


def test_minimal_cover_prefers_single_snapshot():
    cands = {"s1": {"A"}, "s2": {"B"}, "s3": {"A", "B"}}
    assert minimal_cover({"A", "B"}, cands) == ["s3"]


def test_minimal_cover_combines_when_needed():
    cands = {"s1": {"A"}, "s2": {"B"}, "s3": {"C"}}
    assert minimal_cover({"A", "B"}, cands) == ["s1", "s2"]


def test_minimal_cover_impossible_returns_none():
    assert minimal_cover({"Z"}, {"s1": {"A"}}) is None


def test_minimal_cover_empty_required():
    assert minimal_cover(set(), {"s1": {"A"}}) == []


def test_shared_file_must_be_retained_and_explained():
    shared = {"path": "data/shared.parquet", "size_bytes": 500, "partitions": ["A"]}
    s1 = snap("s1", ["A"], files=[shared])
    s2 = snap("s2", ["A"], files=[shared])  # 同一文件被两个快照引用
    plan = compute_plan([s1, s2], [lease("L1", ["A"])], RULES0)
    assert [k["snapshot_id"] for k in plan["keep"]] == ["s1"]  # 最小集合只留一个
    assert plan["releasable_snapshots"] == ["s2"]
    # 共享文件仍被保留集合引用 → 必须保留, 并给出原因
    assert len(plan["shared_files"]) == 1
    sf = plan["shared_files"][0]
    assert sf["path"] == "data/shared.parquet"
    assert sf["must_retain"] and sf["shared"]
    assert "s1" in sf["reason"] and "共享" in sf["reason"]
    # 共享文件不可释放
    assert plan["releasable_files"] == []
    assert plan["totals"]["releasable_bytes"] == 0


def test_releasable_files_only_from_releasable_snapshots():
    s1 = snap("s1", ["A"])
    s2 = snap("s2", ["B"])
    plan = compute_plan([s1, s2], [lease("L1", ["A"])], RULES0)
    assert plan["releasable_snapshots"] == ["s2"]
    assert {f["path"] for f in plan["releasable_files"]} == {"data/s2/0.parquet"}
    assert plan["totals"]["releasable_bytes"] == 100


def test_incomplete_snapshot_excluded_from_cover_and_protected():
    s1 = snap("s1", ["A"], status="IN_PROGRESS")
    plan = compute_plan([s1], [lease("L1", ["A"])], RULES0)
    types = {i["type"] for i in plan["issues"]}
    assert "incomplete_snapshot" in types
    assert plan["protected"][0]["snapshot_id"] == "s1"
    assert plan["releasable_snapshots"] == []  # 未完成快照不可释放


def test_missing_partition_reported():
    s1 = snap("s1", ["A"])
    plan = compute_plan([s1], [lease("L1", ["A", "Z"])], RULES0)
    assert any(i["type"] == "missing_partition" and i["partition"] == "Z"
               for i in plan["issues"])
    assert plan["per_lease"][0]["uncovered"] == ["Z"]


def test_no_manifests_with_active_lease():
    plan = compute_plan([], [lease("L1", ["A"])], RULES0)
    types = {i["type"] for i in plan["issues"]}
    assert "no_manifests" in types and "missing_partition" in types


def test_min_snapshots_rule_keeps_recent():
    snaps = [snap(f"s{i}", [f"P{i}"], created=f"2026-11-0{i}T00:00:00Z") for i in range(1, 4)]
    plan = compute_plan(snaps, [], {"min_snapshots": 2, "protect_days": 0})
    assert {k["snapshot_id"] for k in plan["keep"]} == {"s2", "s3"}  # 最近的两个
    assert plan["releasable_snapshots"] == ["s1"]


def test_protect_days_rule():
    snaps = [snap("old", ["A"], created="2020-01-01T00:00:00Z"),
             snap("new", ["B"], created="2026-11-20T00:00:00Z")]
    plan = compute_plan(snaps, [], {"min_snapshots": 0, "protect_days": 30})
    assert [k["snapshot_id"] for k in plan["keep"]] == ["new"]


def test_per_lease_minimal_sets_union_into_keep():
    s1, s2, s3 = snap("s1", ["A"]), snap("s2", ["B"]), snap("s3", ["C"])
    plan = compute_plan([s1, s2, s3],
                        [lease("L1", ["A"]), lease("L2", ["B"])], RULES0)
    assert {k["snapshot_id"] for k in plan["keep"]} == {"s1", "s2"}
    assert plan["releasable_snapshots"] == ["s3"]
    covers = {l["lease_id"]: [c["snapshot_id"] for c in l["chosen"]]
              for l in plan["per_lease"]}
    assert covers == {"L1": ["s1"], "L2": ["s2"]}


def test_fingerprint_changes_with_registry():
    s1 = snap("s1", ["A"])
    l1 = lease("L1", ["A"])
    base = registry_fingerprint([s1], [l1], RULES0)
    assert base != registry_fingerprint([s1], [lease("L1", ["A"], version=2)], RULES0)
    assert base != registry_fingerprint([s1], [l1], {"min_snapshots": 1, "protect_days": 0})
    s1b = dict(s1, digest="0" * 64)
    assert base != registry_fingerprint([s1b], [l1], RULES0)
    assert base == registry_fingerprint([s1], [l1], RULES0)  # 确定性


def test_coverage_matrix_marks_in_progress():
    s1 = snap("s1", ["A"])
    s2 = snap("s2", ["A", "B"], status="IN_PROGRESS")
    plan = compute_plan([s1, s2], [lease("L1", ["A"])], RULES0)
    rows = {r["partition"]: r for r in plan["coverage"]}
    assert rows["A"]["snapshots"] == {"s1": "complete", "s2": "in_progress"}
    assert rows["B"]["snapshots"] == {"s2": "in_progress"}
    assert rows["A"]["leases"] == ["L1"]
