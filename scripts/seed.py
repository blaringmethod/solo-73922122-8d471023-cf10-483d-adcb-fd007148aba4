#!/usr/bin/env python3
"""向运行中的服务灌入 examples/ 演示数据(仅用标准库)。

用法: python3 scripts/seed.py [base_url] [examples_dir]
默认: http://127.0.0.1:8000 与仓库内 examples/
"""
import json
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
EXAMPLES = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "examples"


def post(path: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()[:120]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:120]


def put(path: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()[:120]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:120]


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
    print(f"\n打开 {BASE}/plans 查看方案")


if __name__ == "__main__":
    main()
