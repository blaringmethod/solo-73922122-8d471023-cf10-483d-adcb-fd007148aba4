"""API 关键场景:上传 → 租约 → 计算 → 确认;缺分区/未完成/缺 manifest 阻断;
登记表变化使方案过期;确认失败不改快照登记。"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def manifest(sid, parts, status="COMPLETE", created="2026-11-01T00:00:00Z",
             extra_files=None):
    files = [
        {"path": f"data/{sid}/{i}.parquet", "size_bytes": 100 * (i + 1),
         "partitions": [p]}
        for i, p in enumerate(parts)
    ]
    files += extra_files or []
    return {"snapshot_id": sid, "created_at": created, "status": status,
            "files": files}


def upload(client, m):
    r = client.post("/api/snapshots", json=m)
    assert r.status_code == 201, r.text
    return r.json()["digest"]


def make_lease(client, lid="L1", parts=("A",)):
    r = client.post("/api/leases", json={
        "lease_id": lid, "name": f"租约{lid}", "required_partitions": list(parts)})
    assert r.status_code == 201, r.text


def compute(client):
    r = client.post("/api/plans")
    assert r.status_code == 201, r.text
    return r.json()


def confirm(client, plan_id):
    return client.post(f"/api/plans/{plan_id}/confirm")


# ---------------------------------------------------------------- 基本流程

def test_happy_path_compute_and_confirm(client):
    upload(client, manifest("s1", ["A", "B"]))
    upload(client, manifest("s2", ["A", "B"]))
    upload(client, manifest("s3", ["C"]))
    make_lease(client, "L1", ("A", "B"))
    make_lease(client, "L2", ("C",))

    plan = compute(client)
    assert plan["status"] == "DRAFT"
    keep = {k["snapshot_id"] for k in plan["payload"]["keep"]}
    assert keep == {"s1", "s3"}                    # 每租约最小集合的并集
    assert plan["payload"]["releasable_snapshots"] == ["s2"]

    r = confirm(client, plan["plan_id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CONFIRMED"
    # 幂等:再次确认仍 200
    assert confirm(client, plan["plan_id"]).status_code == 200
    got = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert got["status"] == "CONFIRMED" and got["confirmed_at"]


def test_snapshot_limit_12(client):
    for i in range(12):
        upload(client, manifest(f"s{i}", ["A"]))
    r = client.post("/api/snapshots", json=manifest("s13", ["A"]))
    assert r.status_code == 400
    assert "12" in r.json()["detail"]
    # 覆盖已有 id 不受限制
    assert client.post("/api/snapshots", json=manifest("s0", ["B"])).status_code == 201


def test_invalid_manifest_rejected(client):
    r = client.post("/api/snapshots", json={"snapshot_id": "x", "files": []})
    assert r.status_code == 422
    r = client.post("/api/snapshots", json={
        "snapshot_id": "x", "created_at": "not-a-time",
        "files": [{"path": "p", "size_bytes": 1, "partitions": ["A"]}]})
    assert r.status_code == 422


# ---------------------------------------------------------------- 阻断场景

def test_missing_partition_blocks_confirm(client):
    upload(client, manifest("s1", ["A"]))
    make_lease(client, "L1", ("A", "Z"))          # Z 无人覆盖
    plan = compute(client)
    assert plan["status"] == "BLOCKED"
    types = {i["type"] for i in plan["payload"]["issues"]}
    assert "missing_partition" in types
    r = confirm(client, plan["plan_id"])
    assert r.status_code == 422
    assert r.json()["detail"]["issues"]


def test_incomplete_only_partition_blocks_confirm(client):
    upload(client, manifest("s1", ["A"]))
    upload(client, manifest("s2", ["B"], status="IN_PROGRESS"))
    make_lease(client, "L1", ("B",))               # B 仅未完成快照覆盖
    plan = compute(client)
    assert plan["status"] == "BLOCKED"
    types = {i["type"] for i in plan["payload"]["issues"]}
    assert "incomplete_snapshot" in types
    assert confirm(client, plan["plan_id"]).status_code == 422
    # 未完成快照受保护, 不可释放
    assert plan["payload"]["releasable_snapshots"] == ["s1"] or \
           "s2" not in plan["payload"]["releasable_snapshots"]


def test_no_manifest_at_all_blocks_confirm(client):
    make_lease(client, "L1", ("A",))
    plan = compute(client)
    assert plan["status"] == "BLOCKED"
    types = {i["type"] for i in plan["payload"]["issues"]}
    assert "no_manifests" in types
    assert confirm(client, plan["plan_id"]).status_code == 422


# ---------------------------------------------------------------- 过期场景

def test_lease_change_stales_plan_and_confirm_fails(client):
    upload(client, manifest("s1", ["A"]))
    make_lease(client, "L1", ("A",))
    plan = compute(client)
    assert plan["status"] == "DRAFT"

    r = client.put("/api/leases/L1", json={
        "lease_id": "L1", "name": "租约L1", "required_partitions": ["A", "B"]})
    assert r.status_code == 200 and r.json()["version"] == 2

    got = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert got["status"] == "STALE"                # 读取时惰性标记过期
    r = confirm(client, plan["plan_id"])
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "STALE"


def test_snapshot_delete_stales_plan_and_registry_untouched(client):
    d1 = upload(client, manifest("s1", ["A"]))
    d2 = upload(client, manifest("s2", ["B"]))
    make_lease(client, "L1", ("A",))
    plan = compute(client)

    assert client.delete("/api/snapshots/s2").status_code == 200
    r = confirm(client, plan["plan_id"])
    assert r.status_code == 409                    # manifest 缺失 → 过期, 确认失败

    # 确认失败不改快照登记:s1 摘要原样, s2 依旧缺失, 方案未确认
    snaps = client.get("/api/snapshots").json()["snapshots"]
    assert [s["snapshot_id"] for s in snaps] == ["s1"]
    assert snaps[0]["digest"] == d1 and snaps[0]["digest"] != d2
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "STALE"


def test_manifest_reupload_changes_digest_and_fails_confirm(client):
    upload(client, manifest("s1", ["A"]))
    make_lease(client, "L1", ("A",))
    plan = compute(client)

    changed = manifest("s1", ["A"],
                       extra_files=[{"path": "data/s1/extra.parquet",
                                     "size_bytes": 1, "partitions": ["A"]}])
    new_digest = upload(client, changed)
    assert new_digest != plan["payload"]["snapshot_digests"]["s1"]

    r = confirm(client, plan["plan_id"])
    assert r.status_code == 409                    # 摘要核对失败, 登记未改动
    snaps = client.get("/api/snapshots").json()["snapshots"]
    assert snaps[0]["digest"] == new_digest        # 登记表保持重传后的状态
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "STALE"


def test_rules_change_stales_plan(client):
    upload(client, manifest("s1", ["A"]))
    make_lease(client, "L1", ("A",))
    plan = compute(client)
    assert client.put("/api/rules", json={"min_snapshots": 1,
                                          "protect_days": 0}).status_code == 200
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "STALE"
    assert confirm(client, plan["plan_id"]).status_code == 409


# ---------------------------------------------------------------- 内容正确性

def test_shared_file_and_releasable_in_payload(client):
    shared = {"path": "data/shared.parquet", "size_bytes": 500, "partitions": ["A"]}
    upload(client, manifest("s1", ["A"], extra_files=[shared]))
    upload(client, manifest("s2", ["A", "B"], extra_files=[shared]))
    make_lease(client, "L1", ("A",))
    plan = compute(client)
    payload = plan["payload"]
    assert payload["totals"]["shared_file_count"] == 1
    sf = payload["shared_files"][0]
    assert sf["path"] == "data/shared.parquet"
    assert set(sf["referenced_by"]) == {"s1", "s2"}
    # 平局决胜: 同为单快照覆盖 A 时, 保留覆盖分区更多的 s2 (A,B)
    assert sf["retained_by"] == ["s2"]
    assert "必须保留" in sf["reason"]
    # s1 独占文件可释放, 共享文件不可释放
    releasable = {f["path"] for f in payload["releasable_files"]}
    assert "data/s1/0.parquet" in releasable
    assert "data/shared.parquet" not in releasable


def test_confirm_unknown_plan_404(client):
    assert confirm(client, "nope").status_code == 404


def test_recompute_after_fix_unblocks(client):
    upload(client, manifest("s1", ["A"]))
    make_lease(client, "L1", ("A", "Z"))
    blocked = compute(client)
    assert blocked["status"] == "BLOCKED"
    upload(client, manifest("s2", ["Z"]))          # 补齐缺失分区
    plan = compute(client)
    assert plan["status"] == "DRAFT"
    assert confirm(client, plan["plan_id"]).status_code == 200
