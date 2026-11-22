# 数据湖快照保留规划

FastAPI + SQLite 的网页工具:数据工程师登记快照 manifest、活动查询租约与保留规则,
系统为每个租约计算覆盖所需分区的**最小快照集合**,解释**共享文件为何必须保留**,
并列出**可释放候选**。缺分区、只有未完成快照覆盖、或缺少 manifest 时方案被阻断,
不能确认。快照 / 租约 / 规则一旦变化,旧方案立即过期(STALE);确认时重新核对
清单摘要,核对失败不改动快照登记。

## 快速开始

### Docker Compose(推荐)

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据持久化在命名卷 `retention-data` 中。

### 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
# 演示数据: python3 scripts/seed.py
```

### 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

## 概念与流程

```
上传 manifest ─┐
创建租约      ─┼─► 计算方案 ─► DRAFT ──确认(重核清单摘要)──► CONFIRMED
设置保留规则  ─┘        │            ▲
                      ▼            │ 登记表变化(快照/租约/规则)
                   BLOCKED ────────┴──────────────► STALE(需重新计算)
```

1. **上传 manifest**(最多 12 个):描述快照状态(COMPLETE / IN_PROGRESS)、
   文件清单及每个文件归属的分区。登记时计算 SHA-256 清单摘要。
2. **创建租约**:声明查询所需分区;每次修改版本号 +1。
3. **保留规则**:至少保留最近 N 个已完成快照、N 天创建保护期;
   未完成快照始终受保护,永不可释放。
4. **计算方案**:
   - 每个租约做**精确最小集合覆盖**(≤12 个快照,子集枚举,平局时优先
     覆盖分区更多的快照,再按 id 字典序,结果确定);
   - 保留集合 = 各租约最小集合的并集 ∪ 规则保留;
   - 文件引用链 文件 → 快照 → 分区 → 租约:被多个快照引用且至少一个
     在保留集合内的共享文件必须保留并给出原因;仅被可释放快照引用的
     文件列入可释放候选并汇总字节数。
5. **确认方案**:重新计算登记表指纹并逐个核对 manifest 摘要;
   指纹不符 → 409 STALE;有校验问题 → 422;摘要缺失/不匹配 → 409。
   **任何确认失败都不会修改快照登记。**

### 阻断确认的情形

- 租约需要的分区没有任何快照覆盖(`missing_partition`)
- 分区仅被未完成(IN_PROGRESS)快照覆盖(`incomplete_snapshot`)
- 有活动租约但一个 manifest 都没传(`no_manifests`)
- 方案生成后快照 / 租约 / 规则发生变化(方案转为 STALE)

## 目录换址

真实数据文件由运维先行复制到新目录,本工具负责把清单里的**旧目录前缀**换成
**新前缀**,以后续查询 / 释放都用新路径。入口在 `/snapshots` 页顶部的
「目录换址」面板,或 `/path-switch` 核对页。

规则:

1. **只启用一对前缀**:新旧前缀规范化后必须**互不包含**(一个不能是另一个的
   前缀,也不能相同);一个库切换成功后只能重提同一对前缀(提示「已切换」),
   启用第二对不同前缀返回 409 `pair_locked`。
2. **核对预览不写库**:核对页展示每个清单的改写明细与「原摘要 → 新摘要」,
   并提前标出冲突。核对期间网页与 API 的新增 / 替换 / 移除照常提交。
3. **切换以生效时已提交清单为准**:切换在 `BEGIN IMMEDIATE` 写事务内重新读取
   登记表并改写全部清单、写入换址记录、把涉及旧路径的草稿置为 STALE,
   提交是原子的。
4. **冲突拒绝**:若届时已有清单引用新目录,或某清单改写后出现重复路径,
   切换返回 409 并点名清单;各清单保留原路径,期间提交的登记仍有效。
5. **迟到清单**:切换后上传的 manifest 若仍用旧前缀,登记时按新前缀转换并
   返回 `rewritten_paths`;转换后出现重复路径则退回(409),不写入。
6. **一致性**:读事务使用 WAL 快照,单次查询 / 方案计算只会看到切换前或
   切换后的一整套路径与清单摘要,不会新旧混杂。
7. **方案**:切换后新路径归并共享文件(同一文件只出现一次);涉及旧路径的
   DRAFT / BLOCKED 方案置为 STALE;已确认(CONFIRMED)方案保持原样,仍展示
   其生成时的原路径。

演示:

```bash
docker compose up --build
python3 scripts/seed.py http://127.0.0.1:8000 examples   # 灌入示例清单并算一个方案
# 浏览器打开 http://localhost:8000/snapshots → 目录换址:
#   旧前缀 lake/orders,新前缀 lake/orders-v2 → 先「核对清单(不写入)」→「执行切换」
# 或一条命令完成演示: python3 scripts/seed.py http://127.0.0.1:8000 examples --switch lake/orders-v2
```

## Manifest 格式

```json
{
  "snapshot_id": "snap-2026-11-02",
  "created_at": "2026-11-02T02:00:00Z",
  "status": "COMPLETE",
  "files": [
    {"path": "lake/orders/dt=2026-11-02/part-0.parquet",
     "size_bytes": 910000,
     "partitions": ["dt=2026-11-02"]}
  ]
}
```

同一 `snapshot_id` 重复上传会替换 manifest 并改变摘要,从而使旧方案过期。

## 页面

| 页面 | 内容 |
|---|---|
| `/` | 仪表盘:登记规模、最新方案状态与校验问题 |
| `/snapshots` | 上传 / 移除 manifest,查看清单摘要与文件分区;顶部「目录换址」入口 |
| `/path-switch` | 换址核对页:按当前已提交清单预览改写明细与冲突(不写入) |
| `/leases` | 租约增删改(修改递增版本) |
| `/rules` | 保留规则 |
| `/plans` + `/plans/{id}` | 方案状态、租约覆盖方案、分区覆盖矩阵、共享文件保留原因、可释放候选、完整文件引用链、确认操作 |

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/snapshots` | 登记 manifest(超过 12 个返回 400) |
| GET/DELETE | `/api/snapshots[/{id}]` | 查询 / 移除 manifest |
| POST/PUT/DELETE | `/api/leases[/{id}]` | 租约管理(PUT 版本 +1) |
| GET/PUT | `/api/rules` | 保留规则 |
| POST | `/api/plans` | 按当前登记表计算方案 |
| GET | `/api/plans[/{id}]` | 方案查询(读取时惰性标记 STALE) |
| POST | `/api/plans/{id}/confirm` | 确认(200 / 409 STALE / 422 阻断) |
| GET | `/api/path-switch` | 当前已启用的前缀对(未切换为 `null`) |
| POST | `/api/path-switch/preview` | 换址核对(不写库,返回改写明细与冲突) |
| POST | `/api/path-switch` | 执行换址(200 已切换 / 409 冲突或已锁定 / 400 前缀非法) |

错误响应的 `detail` 中带结构化原因(issues / mismatches)。

## 设计说明

- **指纹过期机制**:指纹 = 全部快照摘要 + 租约版本 + 规则 的 SHA-256,
  方案生成时落库;读取与确认时重算比对,任何登记变化都会使未确认方案过期。
- **确认不改登记**:确认只把方案状态推进为 CONFIRMED;失败时仅返回错误,
  `snapshots` 表零写入(有测试覆盖)。
- **SQLite 单文件**:默认 `./data/retention.db`,可用环境变量
  `RETENTION_DB` 覆盖(Docker 中为 `/data/retention.db`)。
  数据库以 WAL 模式运行,使读事务在换址期间看到一致快照。
