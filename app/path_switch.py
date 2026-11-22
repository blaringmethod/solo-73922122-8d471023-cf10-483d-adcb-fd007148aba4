"""目录换址服务:核对预览与原子切换。

约定:
- 一个数据库只启用一对互不包含的目录前缀,切换成功后写入 path_switch 单行记录。
- 切换在一个 SQLite 写事务(BEGIN IMMEDIATE)内完成:改写全部清单 → 写记录 →
  让涉及旧路径的草稿过期。任一步失败整体回滚,清单保留原路径,期间提交的登记不受影响。
- 已确认方案不参与改写,仍按 payload 中的原路径展示。
- 切换后迟到上传的旧路径清单在登记时按新路径转换;转换后路径重复则拒绝登记。
"""
from __future__ import annotations

from . import db, paths
from .planner import canonical_digest


def _snapshot_manifests(conn) -> list[dict]:
    return [db.snapshot_obj(r) for r in db.list_snapshots(conn)]


def classify(conn, old_raw: str, new_raw: str) -> dict:
    """规范化前缀并判断本库能否启用这一对。"""
    old, new = paths.validate_pair(old_raw, new_raw)
    current = db.get_path_switch(conn)
    if current is not None:
        if current["old_prefix"] == old and current["new_prefix"] == new:
            return {"status": "already_switched", "old_prefix": old,
                    "new_prefix": new, "current": current,
                    "message": f"目录前缀已切换过: {old} → {new}, 无需重复切换"}
        return {"status": "pair_locked", "old_prefix": old, "new_prefix": new,
                "current": current,
                "message": (f"本库已启用一对目录前缀 {current['old_prefix']} → "
                            f"{current['new_prefix']}, 不能再启用第二对")}
    return {"status": "ok", "old_prefix": old, "new_prefix": new, "current": None}


def preview(conn, old_raw: str, new_raw: str) -> dict:
    """核对清单: 按当前已提交的清单计算改写结果, 不写库。"""
    db.begin(conn)
    try:
        state = classify(conn, old_raw, new_raw)
        if state["status"] != "ok":
            conn.rollback()
            return state
        snapshots = _snapshot_manifests(conn)
        result = paths.build_preview(snapshots, state["old_prefix"], state["new_prefix"])
        conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    result.update(status="preview", old_prefix=state["old_prefix"],
                  new_prefix=state["new_prefix"])
    result["blocked"] = bool(result["conflicts"] or result["duplicates"])
    return result


def apply_switch(conn, old_raw: str, new_raw: str, now: str) -> tuple[int, dict]:
    """原子切换。返回 (HTTP 状态码, 结果)。

    以生效时已提交的清单为准: 事务内重新读取全部清单再改写。
    """
    paths.validate_pair(old_raw, new_raw)     # 先做纯校验, 非法前缀不占事务
    db.begin(conn, write=True)                # 切换与登记/计算/确认互斥
    try:
        state = classify(conn, old_raw, new_raw)
        if state["status"] != "ok":
            conn.rollback()
            return (200 if state["status"] == "already_switched" else 409), state

        old, new = state["old_prefix"], state["new_prefix"]
        snapshots = _snapshot_manifests(conn)
        parsed, conflicts, duplicates = [], [], []
        for s in snapshots:
            manifest = paths.manifest_of(s)
            new_manifest, rewrites, dupes = paths.convert_manifest(manifest, old, new)
            conflict_paths = paths.paths_under(manifest, new)
            if conflict_paths:
                conflicts.append({"snapshot_id": s["snapshot_id"],
                                  "paths": conflict_paths})
            if dupes:
                duplicates.append({"snapshot_id": s["snapshot_id"], "paths": dupes})
            parsed.append((s, new_manifest, rewrites))

        if conflicts or duplicates:
            conn.rollback()
            parts = []
            if conflicts:
                parts.append("已有清单引用新目录: " + ", ".join(
                    f"{c['snapshot_id']} ({len(c['paths'])} 个路径)" for c in conflicts))
            if duplicates:
                parts.append("转换后出现重复路径: " + ", ".join(
                    f"{d['snapshot_id']} ({', '.join(d['paths'])})" for d in duplicates))
            return 409, {
                "status": "conflict", "old_prefix": old, "new_prefix": new,
                "detail": "切换被拒绝 — " + "; ".join(parts),
                "conflicts": conflicts, "duplicates": duplicates,
                "snapshot_count": len(snapshots),
            }

        rewritten_files = 0
        changed_snapshots = []
        for s, new_manifest, rewrites in parsed:
            if not rewrites:
                continue
            digest = canonical_digest(new_manifest)
            # 保留原登记时间, 只改路径/摘要
            db.upsert_snapshot(conn, new_manifest, digest, s["uploaded_at"])
            rewritten_files += len(rewrites)
            changed_snapshots.append({"snapshot_id": s["snapshot_id"],
                                      "old_digest": s["digest"],
                                      "new_digest": digest,
                                      "rewrites": rewrites})

        # 涉及旧路径的草稿即行过期; 已确认方案保持原样(仍展示原路径)。
        stale_plans = []
        for plan in db.list_plans(conn, limit=None):
            if plan["status"] in ("DRAFT", "BLOCKED") and \
                    paths.payload_has_old_paths(plan["payload"], old):
                db.set_plan_status(conn, plan["plan_id"], "STALE")
                stale_plans.append(plan["plan_id"])

        db.set_path_switch(conn, old, new, now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return 200, {
        "status": "switched", "old_prefix": old, "new_prefix": new,
        "switched_at": now,
        "snapshot_count": len(snapshots),
        "changed_snapshot_count": len(changed_snapshots),
        "rewritten_files": rewritten_files,
        "changed_snapshots": changed_snapshots,
        "stale_plans": stale_plans,
        "message": (f"目录前缀已切换: {old} → {new}; 改写 {rewritten_files} 个文件路径, "
                    f"{len(stale_plans)} 个草稿方案已过期"),
    }


def convert_for_registration(conn, manifest: dict) -> tuple[dict, list[dict], dict | None]:
    """登记前按已启用的前缀对转换旧路径。返回 (manifest, 改写明细, 当前换址记录)。"""
    current = db.get_path_switch(conn)
    if current is None:
        return manifest, [], None
    converted, rewrites, duplicates = paths.convert_manifest(
        manifest, current["old_prefix"], current["new_prefix"])
    if duplicates:
        raise paths.DuplicateAfterRewrite(duplicates)
    return converted, rewrites, current
