# 联考阅卷跨点调卷服务

两个可独立重启的 FastAPI 阅卷点。每个服务使用自己的 SQLite 文件，两点之间通过 HTTP 交接答卷。调卷以“单号”为幂等键，保证回执丢失、进程重启后接收点不重复入账，原点也不会重新开放已转走的答卷。

## 1. 启动

```bash
docker compose up --build -d
docker compose ps
```

- 阅卷点 A（默认发起调卷到 B）：<http://localhost:8000/docs>
- 阅卷点 B（接收点，也可反向调 A）：<http://localhost:8001/docs>

停止/重启：

```bash
docker compose restart grading-a
docker compose restart grading-b
docker compose logs -f grading-a grading-b
docker compose down        # 保留命名卷中的 SQLite
docker compose down -v     # 同时删除演示数据
```

容器内数据库分别是：

- A：`/data/grading-a.db`，命名卷 `grading-a-data`
- B：`/data/grading-b.db`，命名卷 `grading-b-data`

Compose 已配置：

- A 内置 `A-001` … `A-008`；
- B 内置 `B-001`、`B-002`；
- B 的未交分卷限额 `MAX_UNSCORED=5`，即初始只剩 3 个接收名额；
- A 的默认接收地址是容器网络内的 `http://grading-b:8000`。

## 2. 在 `/docs` 中的操作顺序

### 2.1 开卷并读取匿名正文

在 A 的 `/docs` 中调用：

```http
POST /papers/A-001/open
```

响应的 `body` 是内置匿名答卷正文；重复调用会返回同一正文，并给出 `already_open: true`。

也可以用请求体：

```http
POST /open
Content-Type: application/json

{"paper_no": "A-001"}
```

### 2.2 交分

```http
POST /papers/A-001/score
Content-Type: application/json

{"score": 88, "comment": "要点完整", "grader": "teacher-1"}
```

也支持：

```http
POST /score
{"paper_no": "A-001", "score": 88}
```

默认分数范围是 `0`–`100`，可用环境变量 `SCORE_MIN`、`SCORE_MAX` 调整。

### 2.3 填单号和一批卷号调卷

在 A 的 `/docs` 调用 `POST /transfers`。因为 Compose 已设置默认 B 地址，请求体可以省略 `target_base_url`：

```json
{
  "order_no": "TR-DEMO-001",
  "paper_nos": ["A-001", "A-002", "A-003"]
}
```

如需显式指定，在宿主机浏览器/Swagger 中也应填写容器网络地址：

```json
{
  "order_no": "TR-DEMO-001",
  "paper_nos": ["A-001", "A-002", "A-003"],
  "target_base_url": "http://grading-b:8000",
  "target_site": "point-b"
}
```

主要响应字段：

```json
{
  "order_no": "TR-DEMO-001",
  "status": "accepted",
  "decision": "accepted",
  "transferred_paper_nos": ["A-002", "A-003"],
  "kept_at_origin_paper_nos": ["A-001"],
  "transferred": ["A-002", "A-003"],
  "kept_at_origin": ["A-001"]
}
```

`transferred` / `kept_at_origin` 是短别名，方便脚本断言。

### 2.4 查看单据

```http
GET /transfers/TR-DEMO-001
POST /transfers/TR-DEMO-001/retry
GET /transfers?status=pending_receiver
POST /transfers/recover-pending
```

接收点可按原单号查回首次决定：

```http
GET  http://localhost:8001/internal/transfers/TR-DEMO-001
POST http://localhost:8001/internal/transfers
```

## 2.5 漏扫补页与版本核对

发现答卷漏扫时，组长在任意一个阅卷点的 `/docs`（标签 **supplements**）按卷号和开卷时看到的版本提交补齐后的全文。已 `open` 或 `scored` 的卷都可以补页；未开卷、调卷暂停中或已转出的卷不能补页。

```http
POST /papers/A-001/supplement
Content-Type: application/json

{
  "seen_version": 1,
  "body": "补齐漏扫页之后的完整正文……",
  "leader": "leader-zhang",
  "reason": "第 3 页漏扫"
}
```

也支持请求体带卷号：`POST /supplement`，字段别名包括 `version/base_version`、`full_body/full_text/content/全文`、`卷号/所见版本` 等。

补页在同一个 `BEGIN IMMEDIATE` 事务内完成：

1. 把补页前的旧正文、当时的分数/评语/阅卷员/交分时间归档到 `paper_revisions`；
2. 用补齐全文替换 `papers.body`；
3. 撤下当前分数（`score/score_comment/grader/scored_at` 置空），状态从 `scored` 退回 `open`，因此卷仍留在当前阅卷点重新待评；
4. `version` 加一（初始为 1），`supplement_count` 加一。

版本与交分规则：

- 开卷响应（以及 `GET /papers/{no}`）包含 `body` 和 `version` / `current_version`。
- **补过页的卷**交分必须带与当前版本一致的 `version`（或 `seen_version/expected_version`）；不带版本返回 `409 score_version_required`，带旧版本返回 `409 score_version_conflict`。
- **未补过页的卷**行为不变，仍接受不带版本的交分；多带的版本字段会被忽略。

旧卷面与当时分数可查：

```http
GET /papers/A-001/revisions      # 别名 /versions、/history、/supplements
GET /papers/A-001/versions/1     # 精确查第 1 版；current/latest 指当前版
GET /papers/A-001                # 详情也内嵌 revisions/history
```

每条记录的 `body`/`old_body` 是旧卷面，`score`/`old_score`/`withdrawn_score` 是补页时撤下的分数，`new_body` 是补入的全文。

限额与幂等：

- 补页**不检查名额**：已交分卷在名额已满时仍可补页。`scored -> open` 后该卷自动进入“未交分”计数，接收新调卷的 `status NOT IN ('scored','transferred_out')` 限额统计会把这些补页待评卷一并计入。
- `paper_revisions` 上有 `UNIQUE(paper_no, version)`，配合事务内版本核对，保证同一卷的同一所见版本只能补页一次；重复提交返回 `409 version_already_supplemented`。
- 任何校验失败都在写入前回滚，卷面、分数和限额计数不变。

旧库升级：服务启动时 `init_db` 会对旧 `papers` 表执行幂等 `ALTER TABLE`（新增 `version DEFAULT 1`、`supplement_count DEFAULT 0`、`last_supplemented_at`），并以 `CREATE TABLE IF NOT EXISTS` 建立 `paper_revisions`，不删除任何现有 SQLite 数据。命名卷 `grading-a-data` / `grading-b-data` 在 `docker compose up --build` 后原地升级。

## 3. 业务状态和一致性设计

### 原点状态

每卷状态为：

- `unopened`：可开卷；
- `open`：已开卷、尚未交分；
- `scored`：已交分；
- `transfer_pending`：已被调卷单选中，接收结果未最终确认，原点禁止开卷/交分；
- `transferred_out`：接收点已确认接收，原点永久禁止再开。

受理调卷的同一个 SQLite `BEGIN IMMEDIATE` 事务中完成：

1. 检查单号是否已经存在；
2. 读取并锁定所选卷；
3. `open` / `scored` 卷列入 `kept_at_origin`，不动其状态，仍可在原点交分；
4. `unopened` 卷改为 `transfer_pending`；
5. 写入 `transfer_orders` 和 `transfer_items`。

因此开卷和调卷并发时只有两种一致结果：

- 开卷事务先提交：调卷单看到已开卷，该卷留在原点；
- 调卷事务先提交：开卷请求看到 `transfer_pending`，返回 `409 paper_transfer_paused`。

### 接收点限额和整批入账

接收点在一个 `BEGIN IMMEDIATE` 事务中：

1. 以 `order_no` 查 `inbound_orders`；若已存在，直接返回首次保存的接收/拒收结果，并标记 `idempotent_replay: true`；
2. 统计 `status NOT IN ('scored','transferred_out')` 的未交分卷数；
3. 若 `当前未交分数 + 本批卷数 > MAX_UNSCORED`，写入 `decision='rejected'` 和 `reason='quota_exceeded'`，不插入任何答卷；
4. 否则在同一事务中写入接收单、明细和全部答卷，新卷状态为 `unopened`，立即可开卷。

如果接收点明确拒收，原点收到 `decision='rejected'` 后把本单候选卷从 `transfer_pending` 恢复为 `unopened`，清空当前调卷单关联；之后可以正常开卷。同一单号永远查回首次拒收结果，即使之后空额出现也不会改变。要在空额出现后重转，应使用新单号。

### 回执丢失、重启和幂等

关键顺序是“原点先持久化暂停状态，再发 HTTP”：

1. 原点提交 `transfer_pending` 和 `pending_receiver` 单据；
2. 调用接收点；
3. 接收点把“单号决定 + 整批答卷”原子提交到自己的 SQLite；
4. 若响应在网络中丢失、原点在第 3 步后重启，原点单据仍是 `pending_receiver`，相关卷仍禁止开卷；
5. 原点重启后的后台对账线程，或人工调用 `POST /transfers/{order_no}/retry`，用同一单号再次请求接收点；
6. 接收点按单号返回原结果，不重复插入；
7. 原点据此把候选卷改为 `transferred_out`，或在拒收时恢复为 `unopened`。

SQLite 使用 `synchronous=FULL`、WAL 和 `busy_timeout`。每个服务只连接自己的 `DATABASE_PATH`，可单独 `docker compose restart`。

## 4. 主要 HTTP 接口

| 用途 | 方法和路径 |
| --- | --- |
| 健康检查/限额 | `GET /health`, `GET /capacity` |
| 列卷/查卷 | `GET /papers`, `GET /papers/{paper_no}` |
| 开卷取正文 | `POST /papers/{paper_no}/open`, `POST /open` |
| 交分（补页卷需带 `version`） | `POST /papers/{paper_no}/score`, `POST /score` |
| 补页（按卷号+所见版本提交全文） | `POST /papers/{paper_no}/supplement`, `POST /supplement` |
| 旧卷面/旧分/版本历史 | `GET /papers/{paper_no}/revisions`（`/versions`、`/history`、`/supplements`） |
| 按版本查卷面 | `GET /papers/{paper_no}/versions/{version\|current}` |
| 发起/重试调卷 | `POST /transfers`, `POST /transfer` |
| 查询调卷单 | `GET /transfers/{order_no}` |
| 显式重试 | `POST /transfers/{order_no}/retry` |
| 批量恢复未知单 | `POST /transfers/recover-pending` |
| 接收点交接 | `POST /internal/transfers` |
| 接收点按单号查询 | `GET /internal/transfers/{order_no}` |

常见别名字段也可识别，例如 `order_id/orderNo/单号`、`paper_ids/paperNumbers/卷号`、`score/points/分数`。

## 5. 已做的实测

本沙箱没有 `docker` / `dockerd` 可执行文件，因此未能在这里实际执行 `docker compose up`；已用两个真实 uvicorn 进程、两个独立 SQLite 文件和一个“吞掉首次回执”的故障代理完成等价 HTTP/重启实测。

安装普通运行依赖后可复跑：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python scripts/live_smoke.py
python scripts/concurrency_check.py
python scripts/supplement_smoke.py
```

`supplement_smoke.py` 用三个真实 uvicorn 进程和独立 SQLite，实测补页后的旧版交分被拒、满额时已交分卷仍可补页、补页待评卷计入接收限额、同版本只能补一次、失败不改卷面/分数/名额，以及“用基线代码建旧库 → 新代码原地迁移并保留数据”。

本次交付前实际运行结果：

```text
[1/2] 开卷/调卷交错与拒收实测通过：
  ✓ 开卷 A-001 并取得正文
  ✓ 调卷时 A-001 已开卷留下，A-002/A-003 转走
  ✓ 已开卷留下的 A-001 在原点交分
  ✓ 原点不能再开已转走的 A-002
  ✓ 接收点开 A-002 取到交接正文并交分
  ✓ 两点其他卷仍可照常开卷、交分
  ✓ 超限额批次被明确拒收，结果列为留在原点
  ✓ 拒收后 A-005 恢复原点开卷资格
  ✓ 原单号重试查回首次拒收，不因后来有空额而改变
  ✓ 新单号在空额出现后可正常整批接收，已开卷 A-005 留下
  ✓ 接收点独立重启后，SQLite 中的拒收结果仍可查回

[2/2] 丢回执 + 原点重启恢复实测通过：
  ✓ 首次交接回执被故障代理吞掉，原点返回“结果未知”
  ✓ 回执丢失后待转卷在原点持续暂停开卷
  ✓ 核对接收点：回执虽丢失，接收决定和两卷已原子入账
  ✓ 原点重启后后台凭原单号自动对账，查回接收点原接收结果
  ✓ 再次用原单号显式重试，原点返回同一最终结果
  ✓ 接收点按单号幂等，重复请求未重复入账
  ✓ 恢复完成后原点不会再开已转走的卷
  ✓ 接收点重启后仍只有两卷，接收记录持久化

LIVE_SMOKE_OK
RACE_OK: 8 opened-and-kept, 16 transferred; no inconsistent outcome
```

并发检查让 24 卷分别同时发起“开卷”和“调卷”。每次运行中“先开卷并留下”和“先调卷并转走”的数量会随调度变化（例如曾测得 11/13，最终复核为 8/16），但结果全部满足：开卷先成功则调卷单保留该卷；调卷先成功则开卷返回 409、接收点恰有一份卷，没有重复或状态不一致。

另外已验证：

- `python scripts/supplement_smoke.py` 全部通过（版本交分、旧卷面/旧分查询、满额补页、限额计入、失败回滚、旧库迁移、重启持久化，共 26 项断言点）；
- 12 线程并发对同一卷同一版本补页，恰有 1 次成功、其余 11 次返回 `version_already_supplemented`，只生成一条历史；
- 原 `live_smoke.py`（`LIVE_SMOKE_OK`）与 `concurrency_check.py`（`RACE_OK`）在改动后仍全部通过；
- `docker-compose.yml` 可被 YAML 解析，包含两个服务和两个独立命名卷；
- 运行依赖可安装到全新目标目录，FastAPI 应用可导入，`/docs` 与 `/openapi.json` 正常；
- `python -m compileall app scripts/supplement_smoke.py` 通过。

## 6. 未完成项与限制

1. **本环境无 Docker 命令/守护进程**，所以没有实际构建镜像或执行 `docker compose up`；已用真实 uvicorn 进程 + 独立 SQLite（含“基线代码建库 → 新代码迁移”）完成等价实测。Compose/Dockerfile 沿用标准 `python:3.12-slim` + uvicorn，未改服务名、端口和命名卷，首次在有 Docker 的环境中仍应跑一次 `docker compose up --build`；命名卷中的旧 SQLite 会在启动时自动加列/建表并保留数据。
2. 未实现阅卷员/组长身份认证与权限区分；`leader` 仅作为补页记录的可选署名。任何能访问 `/docs` 的调用方都可补页，与基线“无认证”的设定一致。
3. 补页后状态复用既有 `open`（已开卷、未交分）表示“重新待评”，没有新增状态值；限额与调卷逻辑因此天然把补页待评卷计入。若上层要求独立的 `regrading` 状态，需要再扩展状态约束与迁移。
4. 分数当前允许对同一版本重复交分更新（沿用基线的宽松策略，响应含 `previously_scored`）；补页会使版本递增，从而旧版本无法再交分。若考试规则要求“一版一次交分不可改”，可再加唯一约束或复核接口。
5. 后台对账默认每 3 秒扫描一次 `pending_receiver`；也可随时调用 retry/recover 接口立即对账。
