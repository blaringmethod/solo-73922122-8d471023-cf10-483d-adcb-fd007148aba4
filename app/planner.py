"""保留方案核心计算:最小集合覆盖、文件引用链、可释放候选、登记表指纹。

快照上限 12 个,因此可以对每个租约做精确的最小集合覆盖(子集枚举,
最坏 2^12 = 4096 种),不必使用近似算法。
"""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timedelta, timezone

MAX_SNAPSHOTS = 12


def canonical_digest(manifest: dict) -> str:
    """manifest 规范化 JSON 的 SHA-256,作为清单摘要。"""
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def registry_fingerprint(snapshots: list[dict], leases: list[dict], rules: dict) -> str:
    """登记表指纹:任一快照摘要、租约版本或保留规则变化都会改变它。"""
    lines = [
        f"snapshot:{s['snapshot_id']}:{s['digest']}"
        for s in sorted(snapshots, key=lambda r: r["snapshot_id"])
    ]
    lines += [
        f"lease:{l['lease_id']}:{l['version']}"
        for l in sorted(leases, key=lambda r: r["lease_id"])
    ]
    lines.append(f"rules:{rules['min_snapshots']}:{rules['protect_days']}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def parse_ts(value: str):
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def minimal_cover(required: set, candidates: dict) -> list | None:
    """精确最小集合覆盖。candidates: {snapshot_id: set(分区)}。

    按 (集合大小, 覆盖总分区数降序, id 字典序) 取最优,保证结果确定。
    无法完全覆盖时返回 None。
    """
    required = set(required)
    useful = sorted(sid for sid, parts in candidates.items() if parts & required)
    for k in range(len(useful) + 1):
        best = None
        for combo in itertools.combinations(useful, k):
            covered = set().union(*(candidates[s] for s in combo)) if combo else set()
            if required <= covered:
                key = (-len(covered), combo)
                if best is None or key < best[0]:
                    best = (key, list(combo))
        if best is not None:
            return best[1]
    return None


def compute_plan(snapshots: list[dict], leases: list[dict], rules: dict,
                 now: datetime | None = None) -> dict:
    """根据当前登记表计算保留方案 payload(纯函数,不写库)。"""
    now = now or datetime.now(timezone.utc)
    issues: list[dict] = []

    # 每个快照覆盖的分区 = 其文件声明分区的并集
    snap_parts: dict[str, set] = {s["snapshot_id"]: set() for s in snapshots}
    file_rows = []  # (path, size_bytes, snapshot_id, 文件的分区集合)
    for s in snapshots:
        for f in s["files"]:
            parts = set(f["partitions"])
            snap_parts[s["snapshot_id"]] |= parts
            file_rows.append((f["path"], f["size_bytes"], s["snapshot_id"], parts))

    complete = {sid: ps for sid, ps in snap_parts.items()
                if _status(snapshots, sid) == "COMPLETE"}
    incomplete = {sid: ps for sid, ps in snap_parts.items()
                  if _status(snapshots, sid) != "COMPLETE"}
    all_parts = set().union(*snap_parts.values()) if snap_parts else set()
    complete_parts = set().union(*complete.values()) if complete else set()

    if leases and not snapshots:
        issues.append({
            "type": "no_manifests",
            "message": "存在活动租约但尚未上传任何快照 manifest",
        })

    # ---- 每个租约的最小快照集合
    per_lease = []
    keep: dict[str, set] = {}
    for lease in leases:
        required = set(lease["required_partitions"])
        missing = sorted(p for p in required if p not in all_parts)
        incomplete_only = sorted(
            p for p in required
            if p not in complete_parts and p in all_parts
        )
        for p in missing:
            issues.append({
                "type": "missing_partition", "lease_id": lease["lease_id"], "partition": p,
                "message": f"租约「{lease['name']}」需要的分区 {p} 没有任何快照覆盖",
            })
        for p in incomplete_only:
            src = sorted(sid for sid, ps in incomplete.items() if p in ps)
            issues.append({
                "type": "incomplete_snapshot", "lease_id": lease["lease_id"],
                "partition": p, "snapshots": src,
                "message": f"分区 {p} 仅由未完成快照 {', '.join(src)} 覆盖, 不能纳入方案",
            })
        coverable = required & complete_parts
        chosen = minimal_cover(coverable, complete) or []
        for sid in chosen:
            keep.setdefault(sid, set()).add(f"租约「{lease['name']}」分区覆盖")
        per_lease.append({
            "lease_id": lease["lease_id"],
            "name": lease["name"],
            "required": sorted(required),
            "uncovered": sorted(required - complete_parts),
            "chosen": [
                {"snapshot_id": sid, "covers": sorted(coverable & complete[sid])}
                for sid in chosen
            ],
        })

    # ---- 保留规则
    complete_sorted = sorted(
        (s for s in snapshots if s["status"] == "COMPLETE"),
        key=lambda s: (s["created_at"], s["snapshot_id"]), reverse=True,
    )
    if rules["min_snapshots"] > 0:
        for s in complete_sorted[: rules["min_snapshots"]]:
            keep.setdefault(s["snapshot_id"], set()).add(
                f"保留规则: 至少保留最近 {rules['min_snapshots']} 个快照")
    if rules["protect_days"] > 0:
        cutoff = now - timedelta(days=rules["protect_days"])
        for s in snapshots:
            created = parse_ts(s["created_at"])
            if created and created >= cutoff:
                keep.setdefault(s["snapshot_id"], set()).add(
                    f"保留规则: 创建于 {rules['protect_days']} 天保护期内")

    # ---- 未完成快照:永远保护,不参与覆盖也不可释放
    protected = {
        s["snapshot_id"]: "快照未完成, 仍在写入, 不可释放"
        for s in snapshots if s["status"] != "COMPLETE"
    }

    retained_ids = set(keep) | set(protected)
    releasable_snaps = sorted(sid for sid in complete if sid not in keep)
    releasable_set = set(releasable_snaps)

    # ---- 文件引用链
    refs: dict[str, set] = {}
    size_of: dict[str, int] = {}
    file_parts: dict[str, set] = {}
    for path, size, sid, parts in file_rows:
        refs.setdefault(path, set()).add(sid)
        size_of[path] = max(size_of.get(path, 0), size)
        file_parts.setdefault(path, set()).update(parts)

    lease_by_part: dict[str, list] = {}
    for lease in leases:
        for p in lease["required_partitions"]:
            lease_by_part.setdefault(p, []).append(lease["name"])

    files, shared_files, releasable_files = [], [], []
    retained_bytes = releasable_bytes = 0
    for path in sorted(refs):
        holders = refs[path]
        retained_by = sorted(holders & retained_ids)
        must_retain = bool(retained_by)
        shared = len(holders) > 1
        part_list = sorted(file_parts[path])
        lease_names = sorted({n for p in part_list for n in lease_by_part.get(p, [])})
        releasable = not must_retain and holders <= releasable_set
        entry = {
            "path": path,
            "size_bytes": size_of[path],
            "referenced_by": sorted(holders),
            "retained_by": retained_by,
            "partitions": part_list,
            "leases": lease_names,
            "shared": shared,
            "must_retain": must_retain,
            "releasable": releasable,
        }
        if must_retain:
            retained_bytes += size_of[path]
            if shared:
                entry["reason"] = (
                    f"被 {len(holders)} 个快照共享 ({', '.join(sorted(holders))}), "
                    f"其中 {', '.join(retained_by)} 在保留集合内"
                    + (f"; 服务租约: {', '.join(lease_names)}" if lease_names else "")
                    + " — 删除会破坏仍被引用的快照, 必须保留"
                )
                shared_files.append(entry)
        elif releasable:
            releasable_bytes += size_of[path]
            releasable_files.append(entry)
        files.append(entry)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "issues": issues,
        "per_lease": per_lease,
        "keep": [{"snapshot_id": sid, "reasons": sorted(rs)}
                 for sid, rs in sorted(keep.items())],
        "protected": [{"snapshot_id": sid, "reason": r}
                      for sid, r in sorted(protected.items())],
        "releasable_snapshots": releasable_snaps,
        "files": files,
        "shared_files": shared_files,
        "releasable_files": releasable_files,
        "totals": {
            "retained_bytes": retained_bytes,
            "releasable_bytes": releasable_bytes,
            "releasable_file_count": len(releasable_files),
            "shared_file_count": len(shared_files),
        },
        "coverage": _coverage_matrix(snapshots, leases, snap_parts),
        "snapshot_digests": {s["snapshot_id"]: s["digest"] for s in snapshots},
    }


def _status(snapshots: list[dict], sid: str) -> str:
    for s in snapshots:
        if s["snapshot_id"] == sid:
            return s["status"]
    return "COMPLETE"


def _coverage_matrix(snapshots: list[dict], leases: list[dict],
                     snap_parts: dict) -> list[dict]:
    parts: set = set()
    for ps in snap_parts.values():
        parts |= ps
    demanded: dict[str, list] = {}
    for lease in leases:
        for p in lease["required_partitions"]:
            parts.add(p)
            demanded.setdefault(p, []).append(lease["name"])
    rows = []
    for p in sorted(parts):
        rows.append({
            "partition": p,
            "leases": sorted(demanded.get(p, [])),
            "snapshots": {
                s["snapshot_id"]: ("complete" if s["status"] == "COMPLETE" else "in_progress")
                for s in snapshots if p in snap_parts[s["snapshot_id"]]
            },
        })
    return rows
