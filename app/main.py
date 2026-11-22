"""FastAPI 入口:网页 + JSON API。

方案生命周期: DRAFT/BLOCKED --(登记表变化)--> STALE; DRAFT --(确认)--> CONFIRMED。
确认时重新核对登记表指纹与每个快照 manifest 摘要;任何失败都只影响方案状态,
绝不修改快照登记。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import db, planner
from .schemas import LeaseIn, Manifest, RulesIn

BASE_DIR = Path(__file__).resolve().parent
MAX_SNAPSHOTS = planner.MAX_SNAPSHOTS


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_partitions(text: str) -> list[str]:
    out, seen = [], set()
    for p in re.split(r"[\s,]+", text.strip()):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("RETENTION_DB", "./data/retention.db")
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db.init_db(db_path)

    app = FastAPI(title="数据湖快照保留规划", version="1.0.0")
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def connect():
        return db.connect(db_path)

    # ------------------------------------------------------------ 辅助
    def ctx(request: Request, **kw):
        base = {
            "request": request,
            "msg": request.query_params.get("msg"),
            "ok": request.query_params.get("ok") == "1",
            "max_snapshots": MAX_SNAPSHOTS,
        }
        base.update(kw)
        return base

    def redirect(url: str, msg: str | None = None, ok: bool = True):
        if msg is not None:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}msg={quote(msg)}&ok={'1' if ok else '0'}"
        return RedirectResponse(url, status_code=303)

    def load_registry(c):
        snapshots = [db.snapshot_obj(r) for r in db.list_snapshots(c)]
        leases = [db.lease_obj(r) for r in db.list_leases(c)]
        rules = db.get_rules(c)
        return snapshots, leases, rules

    def current_fingerprint(c) -> str:
        snapshots, leases, rules = load_registry(c)
        return planner.registry_fingerprint(snapshots, leases, rules)

    def refresh_plan(c, plan: dict) -> dict:
        """登记表已变化时把未确认方案惰性标记为 STALE。"""
        if plan["status"] in ("DRAFT", "BLOCKED"):
            if current_fingerprint(c) != plan["fingerprint"]:
                db.set_plan_status(c, plan["plan_id"], "STALE")
                c.commit()
                plan = db.get_plan(c, plan["plan_id"])
        return plan

    def compute_and_store(c) -> str:
        snapshots, leases, rules = load_registry(c)
        payload = planner.compute_plan(snapshots, leases, rules)
        fp = planner.registry_fingerprint(snapshots, leases, rules)
        plan_id = uuid.uuid4().hex[:12]
        status = "BLOCKED" if payload["issues"] else "DRAFT"
        db.insert_plan(c, plan_id, utcnow(), status, fp, payload)
        c.commit()
        return plan_id

    def do_confirm(c, plan_id: str) -> tuple[int, dict]:
        """确认方案。失败时只返回错误,快照登记保持不变。"""
        plan = db.get_plan(c, plan_id)
        if plan is None:
            return 404, {"detail": "方案不存在"}
        if plan["status"] == "CONFIRMED":
            return 200, {"detail": "方案已确认", "plan_id": plan_id, "status": "CONFIRMED"}
        plan = refresh_plan(c, plan)
        if plan["status"] == "STALE":
            return 409, {"detail": "快照或租约已变化, 方案已过期, 请重新计算",
                         "plan_id": plan_id, "status": "STALE"}
        payload = plan["payload"]
        if payload["issues"]:
            return 422, {"detail": "方案存在校验问题, 不能确认",
                         "issues": payload["issues"],
                         "plan_id": plan_id, "status": plan["status"]}
        # 确认时重新核对每个快照 manifest 摘要
        mismatches = []
        for sid, digest in payload["snapshot_digests"].items():
            row = db.get_snapshot(c, sid)
            if row is None:
                mismatches.append({"snapshot_id": sid, "problem": "manifest 缺失"})
            elif row["digest"] != digest:
                mismatches.append({"snapshot_id": sid, "problem": "manifest 摘要不匹配"})
        if mismatches:
            return 409, {"detail": "清单摘要核对失败, 快照登记未改动",
                         "mismatches": mismatches, "plan_id": plan_id}
        db.set_plan_status(c, plan_id, "CONFIRMED", confirmed_at=utcnow())
        c.commit()
        return 200, {"detail": "方案已确认", "plan_id": plan_id, "status": "CONFIRMED"}

    def register_manifest(c, manifest: Manifest) -> str:
        if (db.get_snapshot(c, manifest.snapshot_id) is None
                and db.count_snapshots(c) >= MAX_SNAPSHOTS):
            raise HTTPException(400, f"最多登记 {MAX_SNAPSHOTS} 个快照 manifest")
        data = manifest.model_dump()
        digest = planner.canonical_digest(data)
        db.upsert_snapshot(c, data, digest, utcnow())
        c.commit()
        return digest

    # ============================================================ 网页
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with closing(connect()) as c:
            snapshots, leases, rules = load_registry(c)
            plans = db.list_plans(c, limit=5)
            latest = refresh_plan(c, plans[0]) if plans else None
        return templates.TemplateResponse(request, "index.html", ctx(
            request, snapshots=snapshots, leases=leases, rules=rules,
            plans=plans, latest=latest))

    # ---- 快照
    @app.get("/snapshots", response_class=HTMLResponse)
    def snapshots_page(request: Request):
        with closing(connect()) as c:
            snapshots, _, _ = load_registry(c)
        return templates.TemplateResponse(request, "snapshots.html", ctx(request, snapshots=snapshots))

    @app.get("/snapshots/{sid}", response_class=HTMLResponse)
    def snapshot_detail(request: Request, sid: str):
        with closing(connect()) as c:
            row = db.get_snapshot(c, sid)
            if row is None:
                raise HTTPException(404, "快照不存在")
            snap = db.snapshot_obj(row)
        return templates.TemplateResponse(request, "snapshot_detail.html", ctx(request, snap=snap))

    @app.post("/web/snapshots")
    async def web_upload_snapshot(request: Request,
                                  manifest_file: UploadFile | None = File(None),
                                  manifest_text: str = Form("")):
        raw = None
        if manifest_file is not None and manifest_file.filename:
            raw = (await manifest_file.read()).decode("utf-8", errors="replace")
        elif manifest_text.strip():
            raw = manifest_text
        if raw is None:
            return redirect("/snapshots", "请上传 manifest 文件或粘贴 JSON", ok=False)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return redirect("/snapshots", f"JSON 解析失败: {e}", ok=False)
        try:
            manifest = Manifest.model_validate(data)
        except ValidationError as e:
            first = e.errors()[0]
            loc = ".".join(str(x) for x in first["loc"])
            return redirect("/snapshots", f"manifest 校验失败: {loc}: {first['msg']}", ok=False)
        with closing(connect()) as c:
            try:
                digest = register_manifest(c, manifest)
            except HTTPException as e:
                return redirect("/snapshots", str(e.detail), ok=False)
        return redirect("/snapshots", f"快照 {manifest.snapshot_id} 已登记 (摘要 {digest[:12]}…)")

    @app.post("/web/snapshots/{sid}/delete")
    def web_delete_snapshot(sid: str):
        with closing(connect()) as c:
            if not db.delete_snapshot(c, sid):
                return redirect("/snapshots", f"快照 {sid} 不存在", ok=False)
            c.commit()
        return redirect("/snapshots", f"快照 {sid} 的 manifest 已移除, 旧方案随之过期")

    # ---- 租约
    @app.get("/leases", response_class=HTMLResponse)
    def leases_page(request: Request):
        with closing(connect()) as c:
            _, leases, _ = load_registry(c)
        return templates.TemplateResponse(request, "leases.html", ctx(request, leases=leases))

    @app.post("/web/leases")
    def web_create_lease(lease_id: str = Form(...), name: str = Form(...),
                         partitions: str = Form(...)):
        parts = parse_partitions(partitions)
        if not lease_id.strip() or not name.strip():
            return redirect("/leases", "租约 ID 与名称不能为空", ok=False)
        if not parts:
            return redirect("/leases", "至少需要一个分区", ok=False)
        with closing(connect()) as c:
            if db.get_lease(c, lease_id.strip()) is not None:
                return redirect("/leases", f"租约 {lease_id} 已存在", ok=False)
            db.insert_lease(c, lease_id.strip(), name.strip(), parts, utcnow())
            c.commit()
        return redirect("/leases", f"租约 {lease_id} 已创建")

    @app.post("/web/leases/{lid}/update")
    def web_update_lease(lid: str, name: str = Form(...), partitions: str = Form(...)):
        parts = parse_partitions(partitions)
        if not parts:
            return redirect("/leases", "至少需要一个分区", ok=False)
        with closing(connect()) as c:
            if not db.update_lease(c, lid, name.strip() or lid, parts, utcnow()):
                return redirect("/leases", f"租约 {lid} 不存在", ok=False)
            c.commit()
        return redirect("/leases", f"租约 {lid} 已更新 (版本+1), 旧方案随之过期")

    @app.post("/web/leases/{lid}/delete")
    def web_delete_lease(lid: str):
        with closing(connect()) as c:
            if not db.delete_lease(c, lid):
                return redirect("/leases", f"租约 {lid} 不存在", ok=False)
            c.commit()
        return redirect("/leases", f"租约 {lid} 已删除, 旧方案随之过期")

    # ---- 保留规则
    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request):
        with closing(connect()) as c:
            rules = db.get_rules(c)
        return templates.TemplateResponse(request, "rules.html", ctx(request, rules=rules))

    @app.post("/web/rules")
    def web_update_rules(min_snapshots: int = Form(0), protect_days: int = Form(0)):
        try:
            rules = RulesIn(min_snapshots=min_snapshots, protect_days=protect_days)
        except ValidationError as e:
            return redirect("/rules", f"规则不合法: {e.errors()[0]['msg']}", ok=False)
        with closing(connect()) as c:
            db.upsert_rules(c, rules.min_snapshots, rules.protect_days, utcnow())
            c.commit()
        return redirect("/rules", "保留规则已更新, 旧方案随之过期")

    # ---- 方案
    @app.get("/plans", response_class=HTMLResponse)
    def plans_page(request: Request):
        with closing(connect()) as c:
            plans = [refresh_plan(c, p) for p in db.list_plans(c)]
        return templates.TemplateResponse(request, "plans.html", ctx(request, plans=plans))

    @app.post("/web/plans/compute")
    def web_compute_plan():
        with closing(connect()) as c:
            plan_id = compute_and_store(c)
        return redirect(f"/plans/{plan_id}", "方案已按当前登记表计算")

    @app.get("/plans/{plan_id}", response_class=HTMLResponse)
    def plan_detail(request: Request, plan_id: str):
        with closing(connect()) as c:
            plan = db.get_plan(c, plan_id)
            if plan is None:
                raise HTTPException(404, "方案不存在")
            plan = refresh_plan(c, plan)
        payload = plan["payload"]
        return templates.TemplateResponse(request, "plan_detail.html", ctx(
            request, plan=plan, p=payload,
            snap_columns=sorted(payload["snapshot_digests"])))

    @app.post("/web/plans/{plan_id}/confirm")
    def web_confirm_plan(plan_id: str):
        with closing(connect()) as c:
            code, body = do_confirm(c, plan_id)
        if code == 404:
            raise HTTPException(404, body["detail"])
        return redirect(f"/plans/{plan_id}", body["detail"], ok=(code == 200))

    # ============================================================ JSON API
    @app.post("/api/snapshots", status_code=201)
    def api_upload_snapshot(manifest: Manifest):
        with closing(connect()) as c:
            digest = register_manifest(c, manifest)
        return {"snapshot_id": manifest.snapshot_id, "digest": digest}

    @app.get("/api/snapshots")
    def api_list_snapshots():
        with closing(connect()) as c:
            snapshots, _, _ = load_registry(c)
        return {"snapshots": snapshots, "count": len(snapshots), "max": MAX_SNAPSHOTS}

    @app.get("/api/snapshots/{sid}")
    def api_get_snapshot(sid: str):
        with closing(connect()) as c:
            row = db.get_snapshot(c, sid)
            if row is None:
                raise HTTPException(404, "快照不存在")
            return db.snapshot_obj(row)

    @app.delete("/api/snapshots/{sid}")
    def api_delete_snapshot(sid: str):
        with closing(connect()) as c:
            if not db.delete_snapshot(c, sid):
                raise HTTPException(404, "快照不存在")
            c.commit()
        return {"deleted": sid}

    @app.post("/api/leases", status_code=201)
    def api_create_lease(lease: LeaseIn):
        with closing(connect()) as c:
            if db.get_lease(c, lease.lease_id) is not None:
                raise HTTPException(409, f"租约 {lease.lease_id} 已存在")
            db.insert_lease(c, lease.lease_id, lease.name,
                            lease.required_partitions, utcnow())
            c.commit()
        return {"lease_id": lease.lease_id, "version": 1}

    @app.get("/api/leases")
    def api_list_leases():
        with closing(connect()) as c:
            _, leases, _ = load_registry(c)
        return {"leases": leases}

    @app.put("/api/leases/{lid}")
    def api_update_lease(lid: str, lease: LeaseIn):
        with closing(connect()) as c:
            if not db.update_lease(c, lid, lease.name, lease.required_partitions, utcnow()):
                raise HTTPException(404, "租约不存在")
            c.commit()
            version = db.get_lease(c, lid)["version"]
        return {"lease_id": lid, "version": version}

    @app.delete("/api/leases/{lid}")
    def api_delete_lease(lid: str):
        with closing(connect()) as c:
            if not db.delete_lease(c, lid):
                raise HTTPException(404, "租约不存在")
            c.commit()
        return {"deleted": lid}

    @app.get("/api/rules")
    def api_get_rules():
        with closing(connect()) as c:
            return db.get_rules(c)

    @app.put("/api/rules")
    def api_put_rules(rules: RulesIn):
        with closing(connect()) as c:
            db.upsert_rules(c, rules.min_snapshots, rules.protect_days, utcnow())
            c.commit()
        return {"min_snapshots": rules.min_snapshots,
                "protect_days": rules.protect_days}

    @app.post("/api/plans", status_code=201)
    def api_compute_plan():
        with closing(connect()) as c:
            plan_id = compute_and_store(c)
            plan = db.get_plan(c, plan_id)
        return plan

    @app.get("/api/plans")
    def api_list_plans():
        with closing(connect()) as c:
            return {"plans": [refresh_plan(c, p) for p in db.list_plans(c)]}

    @app.get("/api/plans/{plan_id}")
    def api_get_plan(plan_id: str):
        with closing(connect()) as c:
            plan = db.get_plan(c, plan_id)
            if plan is None:
                raise HTTPException(404, "方案不存在")
            return refresh_plan(c, plan)

    @app.post("/api/plans/{plan_id}/confirm")
    def api_confirm_plan(plan_id: str):
        with closing(connect()) as c:
            code, body = do_confirm(c, plan_id)
        if code >= 400:
            raise HTTPException(code, body)
        return body

    return app


app = create_app()
