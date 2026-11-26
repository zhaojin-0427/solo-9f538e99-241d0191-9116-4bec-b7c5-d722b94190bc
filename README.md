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

### 2.5 组长补页（漏扫页补齐）

发现漏扫页时，组长在任意阅卷点的 `/docs` 的 **supplements** 分组中，按卷号和
“开卷时看到的版本号”提交补齐后的**完整全文**：

```http
POST /papers/B-001/supplement
Content-Type: application/json

{
  "seen_version": 1,
  "body": "补齐漏扫页之后的完整正文……",
  "operator": "leader-li"
}
```

也可用请求体别名入口 `POST /supplement`（字段支持 `paper_no/卷号`、
`seen_version/version/所见版本`、`body/full_text/全文/补齐后全文` 等），
或 query 形式 `POST /papers/B-001/supplement?seen_version=1&full_text=...`。

成功后：

- `version` 加 1（如 1 → 2），正文整体替换为新全文；
- 当前分数/评语/阅卷员/交分时间被撤下（响应中 `score: null`、
  `score_withdrawn: true`、`withdrawn_score` 是被撤下的旧分）；
- 状态回到 `open`，表示**卷留在当前阅卷点重新待评**，并自动计入未交分限额；
- 旧正文、旧分数和当时的阅卷信息完整归档。

补页后的交分必须带与当前版本一致的 `version`：

```http
POST /papers/B-001/score
{"score": 91, "version": 2}
```

- 带旧版本号（如 `version: 1`）或不带版本 → `409 version_conflict`，
  卷面和分数不变；
- 从未补过页的卷（`version=1`）仍接受原来不带 `version` 的交分。

查旧卷面和当时的分数：

```http
GET /papers/B-001/history            # 补页记录 + 每版快照
GET /papers/B-001/supplements       # 同上（路径别名）
GET /papers/B-001/versions          # 全部版本
GET /papers/B-001/versions/1        # 只看第 1 版正文和当时分数
GET /papers/B-001?version=1         # 等价的便捷写法
GET /papers/B-001/scores            # 历次补页被撤下的分数
```

限额满时的规则：

- **已交分**的卷本来不计入未交分限额；即使当前 `unscored_count == MAX_UNSCORED`
  （名额已满），仍可补页。补页撤分后该卷变为 `open`，限额占用会增加 1，
  因此可能出现 `remaining_quota: -1` 的“超额待评”状态，这是预期行为；
- 此后接收新调卷时，`BEGIN IMMEDIATE` 内的限额统计会把这张补页待评卷一并算入，
  空间不足即整批 `quota_exceeded` 拒收；
- 同一卷的同一个 `seen_version` 只能补页一次（库级
  `UNIQUE(paper_no, seen_version)` + 事务内检查）。重复提交、使用过期版本、
  正文缺失等失败都在同一事务中回滚，**卷面、分数、名额均不变**。

旧版 SQLite（没有 `version` 列）会在容器/进程启动时由 `init_db()` 自动
`ALTER TABLE` 平滑迁移：旧卷一律视为 `version=1`，已有卷面与分数原样保留；
Docker 命名卷中的数据因此可以直接 `docker compose up --build` 升级。

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
| 列卷/查卷 | `GET /papers`, `GET /papers/{paper_no}`（可 `?version=n`） |
| 开卷取正文和版本 | `POST /papers/{paper_no}/open`, `POST /open` |
| 交分（补页卷须带当前 `version`） | `POST /papers/{paper_no}/score`, `POST /score` |
| 组长补页 | `POST /papers/{paper_no}/supplement`, `POST /supplement` |
| 补页历史/旧版卷面与旧分 | `GET /papers/{paper_no}/history`, `.../supplements`, `.../versions[/{n}]`, `.../scores` |
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

python scripts/supplement_check.py
python scripts/live_smoke.py
python scripts/concurrency_check.py
```

`scripts/supplement_check.py` 会先构造一份**旧版 SQLite**（无 `version` 列、
含一卷已交分 88），原地迁移后用两个真实 uvicorn 进程验证补页特性。本次交付前
实际运行结果：

```text
补页特性实测全部通过：
  ✓ 旧版 SQLite 原地迁移到 version/补页表，原有卷面和 88 分保留
  ✓ /docs/openapi 暴露补页和历史接口
  ✓ 开卷返回正文和 version=1，迁移卷的旧分数仍在
  ✓ 补页失败（版本不符/正文缺失）不改卷面、分数和名额
  ✓ 满额（3/3）时已交分 B-001 仍可补页：旧分 88 撤下、变 open，名额变为 4/3
  ✓ 补页后的旧版交分（version=1）和不带版本交分均被 409 拒绝，分数仍空缺
  ✓ 带当前版本 version=2 的交分成功（兼容字符串版本号）
  ✓ 同一答卷的同一版本只能补页一次；重复/过期提交 409 且卷面停在 v3
  ✓ 旧卷面与当时分数（88、93）可经 /history、/versions/{n} 及别名查回
  ✓ 满额补页后接收新调卷：补页待评卷计入限额，A-001 整批被 quota_exceeded 拒收且未入账
  ✓ 腾出名额后新单号整批接收 A-001；旧拒收单 TR-SUP-Q1 仍查回首次拒收
  ✓ 接收点重启后 v3/两条补页历史持久化，旧版交分仍拒绝、当前版交分成功
  ✓ 未补过页的卷（B-004）仍接受不带版本的交分

SUPPLEMENT_CHECK_OK
```

原有两套实测在改动后复跑仍全部通过：

```text
LIVE_SMOKE_OK
RACE_OK: 11 opened-and-kept, 13 transferred; no inconsistent outcome
```

（并发计数每次随调度变化，基线说明中的 8/16、11/13 等均为合法结果。）

本次交付前 `live_smoke.py` 的完整结果：

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

- `docker-compose.yml` 可被 YAML 解析，包含两个服务和两个独立命名卷；
- 运行依赖可安装到全新虚拟环境，FastAPI 应用可导入，`/docs/openapi.json` 包含补页路径；
- `python -m compileall app` 通过；
- 旧版 SQLite 文件可被新版 `init_db()` 原地迁移，数据与旧分保留（`supplement_check.py` 第 0 阶段）。

## 6. 未完成项与限制

1. **本环境无 Docker 命令/守护进程**（无 `/var/run/docker.sock`），所以没有实际构建镜像或执行 `docker compose up`；已用真实 uvicorn 双进程、独立 SQLite 完成等价 HTTP/重启/迁移实测（`scripts/supplement_check.py`、`scripts/live_smoke.py`），并解析校验了 `docker-compose.yml`。Compose 文件和 Dockerfile 沿用标准 `python:3.12-slim` + uvicorn 方式编写，未新增依赖，首次在有 Docker 的环境中应再跑一次 `docker compose up --build`。
2. 未实现阅卷员/组长身份认证与领取锁；需求只要求匿名答卷、开卷取正文/版本、交分和组长补页，因此当前把“开卷”建模为状态转换，不区分具体阅卷员，补页接口也未做组长鉴权。
3. 分数当前允许对同一卷更新（响应中用 `previously_scored` 提示）；补页会撤下当前分，并把旧分、评语、阅卷员、交分时间连同旧正文一起归档到 `paper_supplements`。如果考试规则要求“一次交分不可改”，可再加约束或复核接口。
4. 后台对账默认每 3 秒扫描一次 `pending_receiver`；也可随时调用 retry/recover 接口立即对账。
5. 补页后的状态复用既有 `open`（在本点、已开卷、未交分＝重新待评），没有新增 papers 状态枚举值；另给试卷响应加了 `regrade_pending`、`grading_status=pending_regrade` 字段区分“普通待评”和“补页后重评”。补页会清空当前 `grader`/评语/交分时间，但它们均已在补页历史中留档。
6. 版本号是从 1 开始的整数，每次成功补页 +1；接口兼容 `"v2"` 一类字符串和较多字段/路径别名，但没有实现基于内容哈希的版本令牌，也没有限制补页正文必须与旧文存在包含关系（按“提交补齐后的全文”理解为整体替换）。
