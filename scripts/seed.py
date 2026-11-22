#!/usr/bin/env python3
"""向运行中的服务灌入 examples/ 演示数据(仅用标准库)。

用法: python3 scripts/seed.py [base_url] [examples_dir] [--switch NEW_PREFIX]

默认: http://127.0.0.1:8000 与仓库内 examples/
--switch lake/orders-v2 : 灌数据、算方案后, 把清单里的 lake/orders 前缀换成指定新前缀,
                          并重算一个方案, 用于演示换址。
"""
import json
import sys
import urllib.request
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
SWITCH_NEW = None
if "--switch" in sys.argv:
    SWITCH_NEW = sys.argv[sys.argv.index("--switch") + 1]
    args = [a for a in args if a != SWITCH_NEW]

BASE = args[0] if len(args) > 0 else "http://127.0.0.1:8000"
EXAMPLES = Path(args[1]) if len(args) > 1 else Path(__file__).resolve().parent.parent / "examples"
OLD_PREFIX = "lake/orders"


def request(path: str, payload: dict | None, method: str) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def post(path: str, payload: dict) -> tuple[int, str]:
    return request(path, payload, "POST")


def put(path: str, payload: dict) -> tuple[int, str]:
    return request(path, payload, "PUT")


def main() -> None:
    for f in sorted((EXAMPLES / "manifests").glob("*.json")):
        code, body = post("/api/snapshots", json.loads(f.read_text()))
        print(f"manifest {f.name}: {code} {body}")
    for lease in json.loads((EXAMPLES / "leases.json").read_text()):
        code, body = post("/api/leases", lease)
        print(f"lease {lease['lease_id']}: {code} {body}")
    code, body = put("/api/rules", {"min_snapshots": 1, "protect_days": 0})
    print(f"rules: {code} {body}")
    code, body = post("/api/plans", {})
    print(f"plan: {code} {body}")

    if SWITCH_NEW:
        print("\n--- 目录换址 ---")
        code, body = post("/api/path-switch/preview",
                          {"old_prefix": OLD_PREFIX, "new_prefix": SWITCH_NEW})
        print(f"preview: {code} {body}")
        code, body = post("/api/path-switch",
                          {"old_prefix": OLD_PREFIX, "new_prefix": SWITCH_NEW})
        print(f"switch: {code} {body}")
        code, body = post("/api/plans", {})
        print(f"recomputed plan: {code} {body}")

    print(f"\n打开 {BASE}/snapshots 演示换址, {BASE}/plans 查看方案")


if __name__ == "__main__":
    main()
