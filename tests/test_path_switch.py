"""目录换址:前缀校验、核对预览、原子切换、冲突拒绝、迟到清单转换、
草稿过期/已确认保留、单次查询只看到整套路径。"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.planner import canonical_digest


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


OLD = "lake/orders"
NEW = "lake/orders-v2"


def manifest(sid, parts, prefix=OLD, status="COMPLETE",
             created="2026-11-01T00:00:00Z", extra_files=None, path_style="{p}/{i}.parquet"):
    files = [
        {"path": f"{prefix}/{path_style.format(p=p, i=i)}", "size_bytes": 100 * (i + 1),
         "partitions": [p]}
        for i, p in enumerate(parts)
    ]
    files += extra_files or []
    return {"snapshot_id": sid, "created_at": created, "status": status,
            "files": files}


def upload(client, m, expect=201):
    r = client.post("/api/snapshots", json=m)
    assert r.status_code == expect, r.text
    return r.json()


def switch(client, old=OLD, new=NEW):
    return client.post("/api/path-switch", json={"old_prefix": old, "new_prefix": new})


def paths_of(client, sid):
    return [f["path"] for f in client.get(f"/api/snapshots/{sid}").json()["files"]]


# ---------------------------------------------------------------- 前缀校验

@pytest.mark.parametrize("old,new,code", [
    (OLD, OLD, 400),                  # 相同
    ("lake", "lake/orders", 400),     # old 包含 new
    ("lake/orders", "lake", 400),     # new 包含 old
    ("", "lake/x", 422),              # 空(请求模型校验)
    (OLD, "/", 400),                  # 根目录
])
def test_invalid_prefix_pairs_are_rejected(client, old, new, code):
    r = switch(client, old, new)
    assert r.status_code == code, r.text


def test_normalizes_trailing_slash(client):
    upload(client, manifest("s1", ["A"]))
    r = switch(client, OLD + "/", "./" + NEW + "/")
    assert r.status_code == 200, r.text
    assert r.json()["old_prefix"] == OLD and r.json()["new_prefix"] == NEW
    assert paths_of(client, "s1") == [f"{NEW}/A/0.parquet"]


# ---------------------------------------------------------------- 基本切换

def test_switch_rewrites_all_manifests_and_digests(client):
    d1 = upload(client, manifest("s1", ["A"]))["digest"]
    d2 = upload(client, manifest("s2", ["B"]))["digest"]
    body = switch(client).json()
    assert body["status"] == "switched"
    assert body["rewritten_files"] == 2
    assert paths_of(client, "s1") == [f"{NEW}/A/0.parquet"]
    assert paths_of(client, "s2") == [f"{NEW}/B/0.parquet"]
    got = {s["snapshot_id"]: s["digest"]
           for s in client.get("/api/snapshots").json()["snapshots"]}
    assert got["s1"] != d1 and got["s2"] != d2


def test_only_old_prefixed_paths_rewritten(client):
    other = {"path": "shared/common.parquet", "size_bytes": 7, "partitions": ["A"]}
    upload(client, manifest("s1", ["A"], extra_files=[other]))
    switch(client)
    assert sorted(paths_of(client, "s1")) == sorted(
        [f"{NEW}/A/0.parquet", "shared/common.parquet"])


def test_repeat_same_pair_reports_already_switched(client):
    upload(client, manifest("s1", ["A"]))
    assert switch(client).json()["status"] == "switched"
    r = switch(client)
    assert r.status_code == 200
    assert r.json()["status"] == "already_switched"
    assert "已切换" in r.json()["message"]


def test_library_allows_only_one_pair(client):
    upload(client, manifest("s1", ["A"]))
    assert switch(client).status_code == 200
    r = switch(client, "lake/orders-v2", "lake/orders-v3")
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "pair_locked"


def test_preview_does_not_write(client):
    upload(client, manifest("s1", ["A"]))
    r = client.post("/api/path-switch/preview",
                    json={"old_prefix": OLD, "new_prefix": NEW})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "preview"
    assert body["rewritten_paths"] == 1
    assert body["snapshots"][0]["new_digest"] != body["snapshots"][0]["old_digest"]
    assert paths_of(client, "s1") == [f"{OLD}/A/0.parquet"]
    assert client.get("/api/path-switch").json()["switch"] is None


def test_switch_uses_submitted_manifests_at_effect_time(client):
    """核对期间新增 / 替换 / 移除都能提交;切换以生效时已提交清单为准。"""
    upload(client, manifest("s1", ["A"]))
    upload(client, manifest("s2", ["B"]))
    preview = client.post("/api/path-switch/preview",
                          json={"old_prefix": OLD, "new_prefix": NEW}).json()
    assert preview["snapshot_count"] == 2

    # 核对之后: 新增 s3, 替换 s2, 移除 s1 — 都应能提交
    upload(client, manifest("s3", ["C"]))
    assert upload(client, manifest("s2", ["B", "D"]))["digest"]
    assert client.delete("/api/snapshots/s1").status_code == 200

    body = switch(client).json()
    assert body["snapshot_count"] == 2                   # 以生效时清单为准
    assert {c["snapshot_id"] for c in body["changed_snapshots"]} == {"s2", "s3"}
    assert paths_of(client, "s2") == [f"{NEW}/B/0.parquet", f"{NEW}/D/1.parquet"]
    assert client.get("/api/snapshots/s1").status_code == 404


# ---------------------------------------------------------------- 冲突拒绝

def test_conflict_when_manifest_already_references_new_prefix(client):
    upload(client, manifest("s1", ["A"]))
    upload(client, manifest("s2", ["B"], prefix=NEW))
    before = {s["snapshot_id"]: s["digest"]
              for s in client.get("/api/snapshots").json()["snapshots"]}
    r = switch(client)
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["status"] == "conflict"
    assert [c["snapshot_id"] for c in body["conflicts"]] == ["s2"]
    # 拒绝后清单全部保持原路径
    assert paths_of(client, "s1") == [f"{OLD}/A/0.parquet"]
    assert paths_of(client, "s2") == [f"{NEW}/B/0.parquet"]
    after = {s["snapshot_id"]: s["digest"]
             for s in client.get("/api/snapshots").json()["snapshots"]}
    assert after == before


def test_preview_reports_conflict(client):
    upload(client, manifest("s1", ["A"]))
    upload(client, manifest("s2", ["B"], prefix=NEW))
    body = client.post("/api/path-switch/preview",
                       json={"old_prefix": OLD, "new_prefix": NEW}).json()
    assert [c["snapshot_id"] for c in body["conflicts"]] == ["s2"]


def test_rejected_switch_keeps_concurrent_registrations(client):
    """切换失败时, 期间提交的登记仍有效。"""
    upload(client, manifest("s1", ["A"]))
    upload(client, manifest("s2", ["B"], prefix=NEW))   # 造成冲突
    late = upload(client, manifest("s9", ["Z"]))
    r = switch(client)
    assert r.status_code == 409
    # 冲突前提交的登记没有被回滚
    assert client.get("/api/snapshots/s9").json()["digest"] == late["digest"]
    assert paths_of(client, "s9") == [f"{OLD}/Z/0.parquet"]
    assert client.get("/api/path-switch").json()["switch"] is None


def test_duplicate_after_rewrite_rejected(client):
    """同一清单内既有 old/A 又有 new/A: 既命中冲突, 转换后也重复 → 拒绝切换。"""
    files = [
        {"path": f"{OLD}/A/0.parquet", "size_bytes": 1, "partitions": ["A"]},
        {"path": f"{NEW}/A/0.parquet", "size_bytes": 1, "partitions": ["A"]},
    ]
    r = client.post("/api/snapshots", json={
        "snapshot_id": "s1", "created_at": "2026-11-01T00:00:00Z",
        "status": "COMPLETE", "files": files})
    assert r.status_code == 201
    body = switch(client).json()["detail"]
    assert body["conflicts"] == [{"snapshot_id": "s1",
                                  "paths": [f"{NEW}/A/0.parquet"]}]
    assert body["duplicates"] == [{"snapshot_id": "s1",
                                   "paths": [f"{NEW}/A/0.parquet"]}]


def test_duplicate_after_rewrite_blocks_without_new_reference(client):
    """同一清单里 src/A 改写为 dst/A 后与既有 dst/A 重复 → 拒绝切换, 清单不变。"""
    files = [
        {"path": "src/A/0.parquet", "size_bytes": 1, "partitions": ["A"]},
        {"path": "dst/A/0.parquet", "size_bytes": 1, "partitions": ["A"]},
    ]
    r = client.post("/api/snapshots", json={
        "snapshot_id": "s1", "created_at": "2026-11-01T00:00:00Z",
        "status": "COMPLETE", "files": files})
    assert r.status_code == 201
    resp = switch(client, "src", "dst")
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["duplicates"] == [{"snapshot_id": "s1",
                                   "paths": ["dst/A/0.parquet"]}]
    assert paths_of(client, "s1") == ["src/A/0.parquet", "dst/A/0.parquet"]
    assert client.get("/api/path-switch").json()["switch"] is None


# ---------------------------------------------------------------- 迟到清单

def test_late_manifest_old_paths_registered_as_new(client):
    upload(client, manifest("s1", ["A"]))
    switch(client)
    late = manifest("late", ["L"], prefix=OLD)
    body = upload(client, late)
    assert body["rewritten_paths"] == [
        {"from": f"{OLD}/L/0.parquet", "to": f"{NEW}/L/0.parquet"}]
    assert paths_of(client, "late") == [f"{NEW}/L/0.parquet"]
    # 摘要按转换后的路径计算
    converted = dict(late, files=[dict(late["files"][0], path=f"{NEW}/L/0.parquet")])
    assert body["digest"] == canonical_digest(converted)


def test_late_manifest_duplicate_after_rewrite_is_rejected(client):
    """迟到清单里新旧前缀混用, 转换后同一清单内路径重复 → 退回, 不写入。"""
    upload(client, manifest("s1", ["A"]))
    assert switch(client).status_code == 200
    late_files = [
        {"path": f"{OLD}/A/0.parquet", "size_bytes": 1, "partitions": ["A"]},
        {"path": f"{NEW}/A/0.parquet", "size_bytes": 1, "partitions": ["A"]},
    ]
    r = client.post("/api/snapshots", json={
        "snapshot_id": "late3", "created_at": "2026-11-01T00:00:00Z",
        "status": "COMPLETE", "files": late_files})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["duplicates"] == [f"{NEW}/A/0.parquet"]
    assert client.get("/api/snapshots/late3").status_code == 404


def test_late_manifest_shares_new_path_with_existing_snapshot(client):
    """跨清单的同名文件是合法共享文件(换址后用新路径归并)。"""
    upload(client, manifest("s1", ["A"], path_style="A/part.parquet"))
    assert switch(client).status_code == 200
    late = {"snapshot_id": "late", "created_at": "2026-11-02T00:00:00Z",
            "status": "COMPLETE",
            "files": [{"path": f"{OLD}/A/part.parquet", "size_bytes": 1,
                       "partitions": ["A"]}]}
    body = upload(client, late)
    assert body["rewritten_paths"][0]["to"] == f"{NEW}/A/part.parquet"
    # 两个快照现在引用同一新路径 → 方案里归并为一个共享文件
    client.post("/api/leases", json={"lease_id": "L1", "name": "L1",
                                     "required_partitions": ["A"]})
    plan = client.post("/api/plans").json()
    assert [f["path"] for f in plan["payload"]["shared_files"]] == [f"{NEW}/A/part.parquet"]
    assert plan["payload"]["shared_files"][0]["referenced_by"] == ["late", "s1"]


# ------------------------------------------------------- 方案生命周期与一致性

def test_draft_plan_stales_but_confirmed_plan_keeps_old_paths(client):
    upload(client, manifest("s1", ["A"]))
    client.post("/api/leases", json={"lease_id": "L1", "name": "L1",
                                     "required_partitions": ["A"]})
    stale_plan = client.post("/api/plans").json()
    assert {f["path"] for f in stale_plan["payload"]["files"]} == {f"{OLD}/A/0.parquet"}

    confirmed = client.post("/api/plans").json()
    assert client.post(f"/api/plans/{confirmed['plan_id']}/confirm").status_code == 200

    body = switch(client).json()
    assert stale_plan["plan_id"] in body["stale_plans"]

    got_stale = client.get(f"/api/plans/{stale_plan['plan_id']}").json()
    assert got_stale["status"] == "STALE"

    got_ok = client.get(f"/api/plans/{confirmed['plan_id']}").json()
    assert got_ok["status"] == "CONFIRMED"                    # 已确认方案不被改写
    assert {f["path"] for f in got_ok["payload"]["files"]} == {f"{OLD}/A/0.parquet"}

    # 重新计算的新方案看到新路径
    fresh = client.post("/api/plans").json()
    assert {f["path"] for f in fresh["payload"]["files"]} == {f"{NEW}/A/0.parquet"}


def test_switch_merges_shared_files_under_new_path(client):
    shared = {"path": f"{OLD}/common.parquet", "size_bytes": 42, "partitions": ["A"]}
    upload(client, manifest("s1", ["A"], path_style="s1/{i}.parquet", extra_files=[shared]))
    upload(client, manifest("s2", ["A"], path_style="s2/{i}.parquet", extra_files=[shared]))
    client.post("/api/leases", json={"lease_id": "L1", "name": "L1",
                                     "required_partitions": ["A"]})
    switch(client)
    plan = client.post("/api/plans").json()["payload"]
    assert [f["path"] for f in plan["shared_files"]] == [f"{NEW}/common.parquet"]
    assert plan["shared_files"][0]["referenced_by"] == ["s1", "s2"]


def test_single_snapshot_query_is_consistent_after_switch(client):
    upload(client, manifest("s1", ["A"]))
    upload(client, manifest("s2", ["B"]))
    switch(client)
    for sid in ("s1", "s2"):
        snap = client.get(f"/api/snapshots/{sid}").json()
        assert all(p.startswith(NEW + "/") for p in
                   [f["path"] for f in snap["files"]])
    # 列表与详情一致: 不存在新旧混杂
    for s in client.get("/api/snapshots").json()["snapshots"]:
        assert all(f["path"].startswith(NEW + "/") for f in s["files"])


def test_web_switch_endpoints_follow_redirects(client):
    upload(client, manifest("s1", ["A"]))
    r = client.get("/path-switch", params={"old_prefix": OLD, "new_prefix": NEW})
    assert r.status_code == 200
    assert "核对通过" in r.text and f"{NEW}/A/0.parquet" in r.text
    assert client.get("/api/path-switch").json()["switch"] is None   # 预览不写
    locked = client.get("/path-switch", params={"old_prefix": OLD, "new_prefix": OLD})
    assert locked.status_code == 200 and "不能相同" in locked.text
    overlap = client.get("/path-switch", params={"old_prefix": "lake", "new_prefix": OLD})
    assert overlap.status_code == 200 and "互不包含" in overlap.text
    r = client.post("/web/path-switch", data={"old_prefix": OLD, "new_prefix": NEW},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/path-switch").json()["switch"]["new_prefix"] == NEW


def test_read_transaction_sees_one_consistent_generation(tmp_path):
    """切换期间持有读事务的连接只能看到切换前的一整套路径与摘要。"""
    from app import db, path_switch
    path = str(tmp_path / "gen.db")
    db.init_db(path)
    writer = db.connect(path)
    reader = db.connect(path)
    try:
        for i in range(2):
            m = {"snapshot_id": f"s{i}", "status": "COMPLETE",
                 "created_at": f"2026-11-0{i + 1}T00:00:00Z",
                 "files": [{"path": f"{i}/A/0.parquet", "size_bytes": 1,
                            "partitions": ["A"]}]}
            # 每个快照路径唯一, 切换只改写 prefix==i 的路径
            m["files"][0]["path"] = f"{OLD}/A/part-{i}.parquet"
            db.upsert_snapshot(writer, m, canonical_digest(m), "now")
        writer.commit()

        db.begin(reader)                                  # 读事务开始
        before = [db.snapshot_obj(r) for r in db.list_snapshots(reader)]
        code, body = path_switch.apply_switch(writer, OLD, NEW, "now")
        assert code == 200 and body["status"] == "switched"
        after_in_tx = [db.snapshot_obj(r) for r in db.list_snapshots(reader)]
        reader.rollback()

        assert [s["digest"] for s in before] == [s["digest"] for s in after_in_tx]
        assert all(f["path"].startswith(OLD + "/") for s in after_in_tx for f in s["files"])
        fresh = [db.snapshot_obj(r) for r in db.list_snapshots(reader)]
        assert all(f["path"].startswith(NEW + "/") for s in fresh for f in s["files"])
    finally:
        writer.close()
        reader.close()
