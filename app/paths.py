"""目录换址的纯函数:前缀规范化、互不包含校验、路径改写与核对预览。

只做字符串处理,不访问数据库、不移动真实文件。真实数据由运维先行复制到
新目录,这里负责把 manifest 中的旧目录前缀改写为新前缀。
"""
from __future__ import annotations

from .planner import canonical_digest


class PathSwitchError(ValueError):
    """前缀对不合法(为空、相同或互相包含)。"""


class DuplicateAfterRewrite(ValueError):
    """旧路径按新前缀改写后与清单内已有路径重复。"""

    def __init__(self, duplicates: list[str]):
        super().__init__("转换后出现重复路径: " + ", ".join(duplicates))
        self.duplicates = duplicates


def normalize_prefix(raw: str) -> str:
    """规范化目录前缀: 去空白、去末尾 / 与开头 ./。

    允许绝对路径(以 / 开头),但不允许连续 /、反斜杠或 . / .. 段。
    """
    if not isinstance(raw, str):
        raise PathSwitchError("目录前缀必须是字符串")
    value = raw.strip()
    while value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    if not value:
        raise PathSwitchError("目录前缀不能为空, 也不能是根目录")
    if "\\" in value:
        raise PathSwitchError("目录前缀请使用 / 作为分隔符")
    if "//" in value:
        raise PathSwitchError("目录前缀不能包含连续的 /")
    if any(seg in (".", "..") for seg in value.split("/") if seg):
        raise PathSwitchError("目录前缀不能包含 . 或 .. 段")
    return value


def path_under(path: str, prefix: str) -> bool:
    """path 是否等于 prefix 或位于 prefix 目录之下(按路径段匹配)。"""
    return path == prefix or path.startswith(prefix + "/")


def prefixes_overlap(a: str, b: str) -> bool:
    """两个前缀是否互相包含(含相等)。互不包含才能启用。"""
    return path_under(a, b) or path_under(b, a)


def validate_pair(old_raw: str, new_raw: str) -> tuple[str, str]:
    old = normalize_prefix(old_raw)
    new = normalize_prefix(new_raw)
    if old == new:
        raise PathSwitchError("新旧目录前缀不能相同")
    if prefixes_overlap(old, new):
        raise PathSwitchError("新旧目录前缀必须互不包含(一个不能是另一个的前缀)")
    return old, new


def rewrite_path(path: str, old: str, new: str) -> tuple[str, bool]:
    """把 path 的旧前缀换成新前缀; 返回 (新路径, 是否改写)。"""
    if path == old:
        return new, True
    if path.startswith(old + "/"):
        return new + path[len(old):], True
    return path, False


def manifest_of(snapshot: dict) -> dict:
    """从 snapshot_obj 重建规范 manifest(与上传时的 model_dump 同形)。"""
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "created_at": snapshot["created_at"],
        "status": snapshot["status"],
        "files": snapshot["files"],
    }


def convert_manifest(manifest: dict, old: str, new: str) -> tuple[dict, list[dict], list[str]]:
    """改写 manifest 内所有旧前缀路径。

    返回 (新 manifest, 改写明细, 转换后重复路径)。重复路径由调用方拒绝登记。
    """
    files, rewrites = [], []
    for f in manifest["files"]:
        new_path, changed = rewrite_path(f["path"], old, new)
        files.append(dict(f, path=new_path))
        if changed:
            rewrites.append({"from": f["path"], "to": new_path})
    counts: dict[str, int] = {}
    for f in files:
        counts[f["path"]] = counts.get(f["path"], 0) + 1
    duplicates = sorted(p for p, n in counts.items() if n > 1)
    return dict(manifest, files=files), rewrites, duplicates


def paths_under(manifest: dict, prefix: str) -> list[str]:
    return [f["path"] for f in manifest["files"] if path_under(f["path"], prefix)]


def payload_has_old_paths(payload: dict, old: str) -> bool:
    """方案 payload 中是否出现旧前缀路径(用于换址时让旧草稿过期)。"""
    for entry in payload.get("files", []):
        if path_under(entry.get("path", ""), old):
            return True
    return False


def build_preview(snapshots: list[dict], old: str, new: str) -> dict:
    """核对预览: 每个快照改写后的路径、摘要与冲突, 供网页/API 展示。

    仅做计算, 不写库; 切换生效时会重新按当时登记表计算。
    """
    items, conflicts, duplicates = [], [], []
    rewritten_paths = affected = 0
    for s in snapshots:
        manifest = manifest_of(s)
        new_manifest, rewrites, dupes = convert_manifest(manifest, old, new)
        conflict_paths = paths_under(manifest, new)
        if conflict_paths:
            conflicts.append({"snapshot_id": s["snapshot_id"], "paths": conflict_paths})
        if dupes:
            duplicates.append({"snapshot_id": s["snapshot_id"], "paths": dupes})
        if rewrites:
            affected += 1
        rewritten_paths += len(rewrites)
        items.append({
            "snapshot_id": s["snapshot_id"],
            "status": s["status"],
            "old_digest": s["digest"],
            "new_digest": canonical_digest(new_manifest),
            "rewrites": rewrites,
            "conflict_paths": conflict_paths,
            "duplicate_paths": dupes,
        })
    return {
        "snapshot_count": len(snapshots),
        "affected_snapshots": affected,
        "rewritten_paths": rewritten_paths,
        "conflicts": conflicts,
        "duplicates": duplicates,
        "snapshots": items,
    }
