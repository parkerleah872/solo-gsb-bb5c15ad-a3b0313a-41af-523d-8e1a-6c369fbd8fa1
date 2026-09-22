# 合同文本脱敏 API（Python / FastAPI）

提交合同原文及一批带 **起止位置、优先级、类别** 的敏感片段，服务端：

1. 按 **原始 Unicode 码位位置** 一次性解析全部片段，确定性消解重叠；
2. 生成 **稳定占位符**（同文档内相同「类别 + 原文」复用同一占位符）；
3. 原文与片段映射经 **Fernet 对称加密** 后存入 SQLite，且 **可过期**（定时 + 懒清理）；
4. 恢复接口仅认 **一次性令牌**：用后作废并轮换，只能还原请求中显式指定的占位符。

此外支持**可复用的自动脱敏策略**：一条策略由多条规则（`rule_id`、类别、正则、
优先级）组成，创建后不可变。提交文档时可传 `policy_id`，服务端直接在 Unicode
原文上按规则扫描出自动片段，与客户端 `spans` 合并后沿用同一套重叠排序；正则
禁止空匹配，并对**单条规则设置执行超时**，任一规则超时则整次提交 `422` 且不
写入文档。

---

## 一、单命令启动（Docker Compose）

```bash
docker compose up --build -d
```

- 服务地址：http://localhost:8000
- 交互式文档（Swagger）：http://localhost:8000/docs
- 健康检查：`GET /health`

> 开箱即用的默认主密钥仅用于快速体验；生产环境请先生成并注入自己的密钥：
>
> ```bash
> docker compose run --rm api python -m app.cli keygen
> REDACT_MASTER_KEY=<输出的密钥> docker compose up --build -d
> ```
>
> 也可复制 `.env.example` 为 `.env` 后修改，Compose 会自动加载。

停止 / 清理：

```bash
docker compose down            # 停止（数据保留在命名卷 redaction-data 中）
docker compose down -v         # 同时删除数据卷
```

---

## 二、本地运行（不用 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1) 生成主密钥（可选；未设置时开发环境会生成一次性密钥，重启后旧数据无法解密）
export REDACT_MASTER_KEY="$(python -m app.cli keygen)"

# 2) 初始化 SQLite（幂等，应用启动时也会自动建表）
export REDACT_DATABASE_URL="sqlite:///./data/redaction.db"
python -m app.cli initdb

# 3) 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

测试：

```bash
.venv/bin/pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 三、API 说明

所有位置偏移均为 **Unicode 码位索引（即 Python 对字符串的索引方式），左闭右开**，
不是 UTF-8 字节偏移。例如 `"合同🎉甲方张三"` 中 `张三` 的位置是 `[5, 7)`。

### 1. 提交脱敏 `POST /documents`

请求：

```json
{
  "text": "甲方张三向乙方李四借款人民币10000元。",
  "ttl": 3600,
  "policy_id": null,
  "spans": [
    {"start": 2, "end": 4,  "priority": 5, "category": "person"},
    {"start": 7, "end": 9,  "priority": 5, "category": "PERSON"},
    {"start": 11, "end": 20, "priority": 3, "category": "MONEY"},
    {"start": 2, "end": 9,  "priority": 0, "category": "BLOCK"}
  ]
}
```

说明：

- `spans` 与 `policy_id` **至少提供其一**；只传 `policy_id` 时完全由规则生成片段；
- 自动片段同样使用 Unicode 码位偏移，与人工片段统一进入下面的确定性重叠排序；
- 当 **优先级、起止位置、长度完全相同** 时，**人工片段优先**于自动片段；
- 响应中每个片段带 `source`（`"manual"` 手传 / `"auto"` 规则生成）与 `rule_id`
  （仅 `auto` 片段标注来源规则）；
- 每个片段还带**冲突溯源**字段（详见第四节）：`fragment_id`（按合并后原始顺序
  分配的响应内唯一编号）、`decision_rank`（优先级排序后的决策次序）、
  `blocked_by_fragment_id`（丢弃片段实际被哪个已落位片段阻断）。

响应（节选）：

```json
{
  "document_id": "0b0b3e4b…",
  "redacted_text": "甲方{{PERSON:01}}向乙方{{PERSON:02}}借款{{MONEY:01}}。",
  "fragments": [
    {"fragment_id": 1, "decision_rank": 1, "blocked_by_fragment_id": null,
     "placeholder": "{{PERSON:01}}", "category": "PERSON", "start": 2, "end": 4,
     "priority": 5, "kept": true,  "drop_reason": null, "source": "manual", "rule_id": null},
    {"fragment_id": 2, "decision_rank": 2, "blocked_by_fragment_id": null,
     "placeholder": "{{PERSON:02}}", "category": "PERSON", "start": 7, "end": 9,
     "priority": 5, "kept": true,  "drop_reason": null, "source": "auto", "rule_id": "person-name"},
    {"fragment_id": 3, "decision_rank": 3, "blocked_by_fragment_id": null,
     "placeholder": "{{MONEY:01}}", "category": "MONEY", "start": 11, "end": 20,
     "priority": 3, "kept": true,  "drop_reason": null, "source": "auto", "rule_id": "money"},
    {"fragment_id": 4, "decision_rank": 4, "blocked_by_fragment_id": 1,
     "placeholder": "", "category": "BLOCK", "start": 2, "end": 9,
     "priority": 0, "kept": false, "drop_reason": "overlaps_higher_priority",
     "source": "manual", "rule_id": null}
  ],
  "restore_token": "6c538c2c…（64 位十六进制，仅此一次返回明文）",
  "expires_at": "2026-09-21T12:00:00.000000Z",
  "token_expires_at": "2026-09-21T11:10:00.000000Z"
}
```

### 2. 创建自动脱敏策略 `POST /policies`

策略**创建后不可变**（不提供修改 / 删除接口）；每条规则含 `rule_id`（策略内
唯一）、`category`、`pattern`（Python Unicode 正则）、`priority`。

```json
{
  "name": "合同 PII（可选，仅便于辨识）",
  "rules": [
    {"rule_id": "person-name", "category": "person", "pattern": "张三|李四", "priority": 5},
    {"rule_id": "money",       "category": "money",  "pattern": "人民币\\d+元", "priority": 3},
    {"rule_id": "phone",       "category": "phone",  "pattern": "1\\d{10}",     "priority": 2}
  ]
}
```

- `rule_id`：1–64 字符，`[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`，同一策略内唯一；
- 类别同样会 `strip + 大写`，须匹配 `[A-Z][A-Z0-9_]{0,31}`；
- 正则在创建时编译校验：**无法编译（422）、可能产生空匹配（422）都会被拒绝**。
  例如 `a*`、`x?`、`(foo)?` 这类能匹配零个码位的模式一律不允许；
- 响应 `201`，返回 `policy_id` 与按提交顺序保存的规则列表。

### 3. 查询自动脱敏策略 `GET /policies/{policy_id}`

返回策略元信息与全部规则（按创建顺序）；不存在返回 `404`。

### 4. 查询脱敏结果 `GET /documents/{document_id}`

返回脱敏文本与全部片段明细（含被丢弃片段及原因、`source` / `rule_id` 标注、
`fragment_id` / `decision_rank` / `blocked_by_fragment_id` 溯源字段，按
`fragment_id` 排序）。
不返回任何原文信息，不需要令牌。文档过期后返回 `410 Gone`，不存在返回 `404`。

### 5. 一次性令牌恢复 `POST /restore`

```json
{
  "token": "6c538c2c…",
  "placeholders": ["{{MONEY:01}}"]
}
```

```json
{
  "restored": [
    {
      "placeholder": "{{MONEY:01}}",
      "category": "MONEY",
      "value": "人民币10000元",
      "occurrences": [
        {"start": 11, "end": 20, "priority": 3, "source": "auto", "rule_id": "money"}
      ]
    }
  ],
  "restore_token": "新的一次性令牌…",
  "token_expires_at": "…"
}
```

规则：

- 令牌只保存 **SHA-256 哈希**，明文仅在创建 / 轮换时出现一次；
- 每次恢复在单个 SQLite 事务中完成：旧令牌置 `used=1` 作废，返回轮换后的新令牌；
- 令牌默认 10 分钟有效（`REDACT_TOKEN_TTL`），且令牌有效期不超过文档 TTL；
- 仅能还原请求中显式列出的、属于该文档的占位符（复用占位符时 `occurrences`
  会返回全部出现位置，并逐一标注 `source` / `rule_id`）；
- 令牌无效 / 已用 / 过期 → `401`；占位符不属于文档 → `400`；文档已过期 → `410`。

### 6. 健康检查 `GET /health` → `{"status": "ok"}`

---

## 四、重叠消解与占位符规则

排序键（全部确定，无随机）：

1. `priority` **数值大者优先**；
2. 同优先级：`start` 小者优先；
3. 仍相同：片段长者优先；
4. 仍相同：**人工片段（`manual`）优先于自动片段（`auto`）**；
5. 仍相同：按提交顺序（人工片段按 `spans` 顺序，自动片段接在其后，先到先得）。

排序后依次落位：与**任何已占用区间相交**的片段被丢弃，
`drop_reason = "overlaps_higher_priority"`（同优先级被先到者占用也使用此标记）。

### 片段冲突溯源字段

每次提交为**人工与自动候选合并后的全部片段**分配三个溯源字段，提交响应
（`POST /documents`）与文档查询（`GET /documents/{id}`）均返回，且落库后不再漂移：

- `fragment_id`：响应内唯一编号（从 1 起），按**合并后的原始顺序**分配——
  人工片段按 `spans` 顺序编号，自动候选（规则按策略顺序扫描、按出现位置命中）
  接在其后。**完全重复的候选也各有独立 `fragment_id`**；
- `decision_rank`：按上面的优先级键排序后、依次落位决策的次序（从 1 起）。
  它可能与 `fragment_id` 不同（后提交的高优先级片段先决策）；
- `blocked_by_fragment_id`：保留片段恒为 `null`；丢弃片段指向**当时已经落位
  且与之相交的第一个片段**的 `fragment_id`——即沿决策先后（`decision_rank`
  从小到大）遇到的首个相交者，也就是实际导致该候选被拒绝的那一个，而不是
  事后从所有相交项中任取。若丢弃片段同时跨越多个已保留区间，取其中决策最早者。
  既有 `drop_reason` 语义保持不变。

示例：一个候选同时跨越两个已保留区间——

```jsonc
// text = "0123456789ABCDEF"
spans: [
  {"start": 0, "end": 8, "priority": 1, "category": "C"},  // fragment_id 1
  {"start": 2, "end": 4, "priority": 9, "category": "A"},  // fragment_id 2，decision_rank 1（先落位）
  {"start": 6, "end": 8, "priority": 9, "category": "B"}   // fragment_id 3，decision_rank 2（随后落位）
]
// fragment_id 1 的大区间与 2、3 都相交，最后决策（decision_rank 3）时被拒；
// blocked_by_fragment_id = 2 —— 取决策最早的相交者，而非 start 更小或任意一个。
// 渲染结果：01{{A:01}}45{{B:01}}89ABCDEF
```

跨来源时同理：自动候选虽排在人工之后（`fragment_id` 更大），只要优先级更高就
会先落位，被其拒绝的人工片段 `blocked_by_fragment_id` 指向该自动片段；同起止、
同长度、同优先级时人工先落位，自动片段反向指向人工片段。

占位符：

- 形如 `{{类别:两位序号}}`，如 `{{PERSON:01}}`；序号超过 99 自动加宽；
- 类别先 `strip + 大写`，须匹配 `[A-Z][A-Z0-9_]{0,31}`；
- 同文档内 `(类别, 原文)` 完全相同的片段复用同一占位符；不同文档不复用；
  人工与自动片段只要 `(类别, 原文)` 相同也复用同一占位符；
- 渲染替换自后向前进行，保证前序片段的位置不失效。

### 自动片段的执行与安全语义

- 规则使用 Python 内置 `re`（默认 Unicode 语义）直接在 **码位原文** 上扫描，
  命中的 `start()/end()` 即文档偏移；
- **空匹配拒绝**：创建策略时会先在空串上试匹配，运行时再对每次命中兜底校验，
  任何零宽度命中都会使提交 `422`；
- **单规则超时**：每条规则在独立的常驻**工作进程**中执行（`re` 的灾难性回溯
  发生在不释放 GIL 的 C 层，进程内无法可靠中断），超过 `REDACT_RULE_TIMEOUT`
  秒（默认 1.0）即杀死该进程并于下次调用重建；
- **任一规则超时 / 空匹配 / 命中数超上限，整次提交返回 `422` 且不写入文档**
  （不生成 document、不落任何片段、不签发恢复令牌）；
- 策略不存在 → `404`；策略本身只保存不敏感的正则规则，不接触文档原文。

---

## 五、数据安全与过期

- 文档原文、每个保留片段的原文均以 **Fernet（AES-128-CBC + HMAC，cryptography 库）**
  加密后落库；数据库文件中无明文；
- 主密钥通过 `REDACT_MASTER_KEY` 提供（支持 Fernet 密钥，或任意口令经 SHA-256 派生）；
  `REDACT_ENV=production` 时未配置密钥会直接拒绝启动；
- 文档映射默认 1 天过期（提交时可用 `ttl` 指定，1..604800 秒），
  过期后由后台任务（默认每 300 秒）及访问时的懒检查物理删除；
- 恢复令牌默认 10 分钟过期，过期 / 已用即删或标记作废；
- 自动脱敏策略不可变、不加密（仅正则规则），随卷持久化；删除数据卷即一并清除。

**生产建议**：将 SQLite 数据卷纳入磁盘加密与备份策略；服务部署于内网或加 TLS / 鉴权网关；
按最小需要设置 `REDACT_DEFAULT_TTL` / `REDACT_TOKEN_TTL` / `REDACT_RULE_TIMEOUT`；
密钥通过环境变量或密钥管理系统注入，不要写入镜像。

---

## 六、配置项（环境变量，前缀 `REDACT_`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REDACT_DATABASE_URL` | `/data/redaction.db` | SQLite 路径，支持 `sqlite:///` 前缀 |
| `REDACT_MASTER_KEY` | 空 | Fernet 密钥或任意口令；生产必填 |
| `REDACT_ENV` | `development` | 设为 `production` 强制要求密钥 |
| `REDACT_DEFAULT_TTL` | `86400` | 文档映射默认有效期（秒） |
| `REDACT_MAX_TTL` | `604800` | 允许提交的最大 TTL（秒） |
| `REDACT_TOKEN_TTL` | `600` | 一次性恢复令牌有效期（秒） |
| `REDACT_RULE_TIMEOUT` | `1.0` | 单条自动规则执行超时（秒）；任一超时则整次提交 422 |
| `REDACT_CLEANUP_INTERVAL` | `300` | 后台过期清理周期（秒） |

---

## 七、项目结构

```
app/
  main.py               FastAPI 入口、生命周期、后台清理、异常映射
  config.py             环境变量配置
  crypto.py             Fernet 加密封装、一次性令牌生成
  db.py                 SQLite 建表（幂等，含旧库迁移）与连接
  redaction.py          重叠消解 / 稳定占位符 / 渲染 / 冲突溯源（纯函数，易测）
  policy_engine.py      规则编译、空匹配拒绝、单规则超时（独立工作进程）
  schemas.py            请求与响应模型（含策略、片段来源标注、冲突溯源字段）
  services/
    document_service.py 提交、查询、恢复、过期清理业务逻辑
    policy_service.py   自动脱敏策略创建 / 查询（不可变）
    timeutil.py         ISO-8601 UTC 时间工具
    errors.py           服务层错误 -> HTTP 状态码
  routers/__init__.py    /policies、/documents、/documents/{id}、/restore、/health
  cli.py                keygen / initdb 命令
tests/                  核心算法单元测试 + API 端到端测试（含超时 / 空匹配）
Dockerfile              非 root 运行、内置健康检查
docker-compose.yml      单命令启动 + 持久化命名卷
```
