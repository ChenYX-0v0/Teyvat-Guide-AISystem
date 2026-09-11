# teyvat-ai · AI 派蒙服务

基于 **LangChain + DeepSeek-V4.1-Flash** 的独立 AI 服务，为 `teyvat-guide` 主服务提供模型调用能力。

- 对外接口：`GET /health`、`POST /v1/complete`、`POST /v1/complete/stream`
- **无状态**：不落业务库、不建用户体系、不管会话存储；重启即恢复，可水平复制
- 与主服务**完全独立**：不共享代码、不共享数据库，唯一耦合面是 HTTP 契约

## 文档

完整索引见 [`doc/README.md`](doc/README.md)。

| # | 文档 | 什么时候看 |
| --- | --- | --- |
| 01 | [接口契约](doc/01-接口契约.md) | 对接前必读：字段、错误码、SSE 协议、兼容性承诺 |
| 02 | [AI 派蒙架构设计（DeepSeek）](doc/02-AI派蒙架构设计-DeepSeek方案.md) | 想了解设计取舍、人格与上下文 |
| 03 | [主服务对接说明](doc/03-主服务对接说明.md) | 要把本服务接进主服务（当前阻塞项） |
| 04 | [部署运维接入说明](doc/04-部署运维接入说明.md) | 部署上线、验收、排障 |
| 05 | [开发路线图](doc/05-开发路线图.md) | 看待办排期、里程碑与决策点 |
| 06 | [阿里云部署方案](doc/06-阿里云部署方案.md) | 目标环境是阿里云时 |

---

## 1. 快速开始

```powershell
cd ai-service

# 1) 虚拟环境
python -m venv .venv

# 2) 依赖（P0/P1 只需要核心依赖）
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3) 配置
Copy-Item .env.example .env
# 然后编辑 .env，至少填写两项：
#   AI_SERVICE_KEY     自行随机生成：
#     .\.venv\Scripts\python.exe -c "import secrets;print(secrets.token_urlsafe(32))"
#   DEEPSEEK_API_KEY   你的 DeepSeek 密钥

# 4) 启动
.\.venv\Scripts\python.exe -m app.main

# 开发时热重载（改代码自动重启）
$env:AI_RELOAD = "1"; .\.venv\Scripts\python.exe -m app.main
```

> 启动入口只有 `python -m app.main`（跨平台，会读取 `.env` 里的 host/port）。
> **不要**用裸命令 `uvicorn app.main:app`——它会忽略 `.env` 的端口配置，默认跑在 8000。

启动自检不通过会**直接失败**（缺密钥、缺上游 key 等），不会带病运行。

### 联调验证

```powershell
cd ai-service

# 1) 读取本服务密钥（主服务那边要填同一个值）
$KEY = (((Get-Content -Encoding UTF8 .env) -match '^AI_SERVICE_KEY=') -replace '^AI_SERVICE_KEY=','').Trim()

# 2) 健康检查（无需鉴权）
Invoke-RestMethod http://127.0.0.1:8090/health | ConvertTo-Json -Depth 5

# 3) 真实补全
$body = @{ prompt = '你好呀派蒙'; options = @{ profile = 'chat' } } | ConvertTo-Json -Compress
$r = Invoke-RestMethod -Uri http://127.0.0.1:8090/v1/complete -Method Post `
  -Headers @{ 'X-AI-Key' = $KEY } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$r.data.reply
$r.data.tokens
```

**最省事的验证方式**（绕开 PowerShell 的引号与编码坑，推荐）：

```powershell
.\.venv\Scripts\python.exe -c "import httpx,pathlib;env=pathlib.Path('.env').read_text(encoding='utf-8');k=next(l.split('=',1)[1].strip() for l in env.splitlines() if l.startswith('AI_SERVICE_KEY='));r=httpx.post('http://127.0.0.1:8090/v1/complete',json={'prompt':'你好呀派蒙','options':{'profile':'chat'}},headers={'X-AI-Key':k},timeout=120);d=r.json()['data'];print(d['reply']);print(d['tokens'])"
```

#### 三个已经踩过的坑

| 坑 | 现象 | 做法 |
| --- | --- | --- |
| `(...)[0].Split(...)` | `[System.Char] 不包含名为 Split 的方法` | 对单个字符串取 `[0]` 得到的是一个**字符**；用 `-match` 过滤 + `-replace` 提取（见上面第 1 步） |
| 控制台中文乱码 | reply 显示成 `ä½ å¥½...` | **只是显示问题**，服务端返回的是正确 UTF-8（用 Python 客户端可验证）。执行 `chcp 65001` 即可 |
| `curl.exe` + `\"` 手写 JSON | 401 或 400 | `\"` 是 bash 写法，PowerShell 单引号内不处理反斜杠，JSON 非法。用 `ConvertTo-Json` 生成，`-Body` 传 UTF-8 字节 |

> 非要 `curl.exe` 的话，把 body 写进文件再引用：
> `curl.exe -X POST http://127.0.0.1:8090/v1/complete -H "Content-Type: application/json" -H "X-AI-Key: $KEY" --data-binary "@payload.json"`

### 测试

```powershell
pytest -q
```

单测不访问网络；接口层测试会把 LLM 工厂替换为假实现。

---

## 2. 模型与参数（重要）

DeepSeek **只有一个模型 ID `deepseek-flash`**，"是否推理"由参数控制，不是换模型：

```json
{ "model": "deepseek-flash",
  "thinking": { "type": "enabled" | "disabled",
                "reasoning_effort": "none" | "low" | "high" | "max" } }
```

因此本服务用「**推理档位 profile**」代替模型路由：

| profile | thinking | temperature | max_tokens | 适用 |
| --- | --- | --- | --- | --- |
| `chat` | disabled | 0.85 | 1024 | 派蒙日常闲聊 / 答疑（**必须关思考才能调语气**） |
| `analysis` | enabled, high | 不发 | 4096 | 养成数据分析、配队、词条收益 |
| `tool` | disabled | 0.2 | 2048 | 工具调用（思考模式不支持 `tool_choice=required`） |
| `json` | disabled | 0.1 | 2048 | 结构化输出 |

> ⚠️ 思考模式下 `temperature` **完全无效**，服务会显式丢弃并打日志，避免误以为生效。

档位选择优先级：请求显式 `options.profile` > `options.thinking` > 意图关键词启发式 > `LLM_DEFAULT_PROFILE`。

### 前缀缓存（省钱关键）

DeepSeek 缓存命中的输入单价约是未命中的 **1/50**（$0.003 vs $0.15 每 1M tokens）。

因此 system 提示词按「**稳定在前、动态在后**」组装：

```
paimon_base → paimon_rules → paimon_fewshot → 主服务补充指令 → 玩家上下文 → RAG 资料
   └──────────── 稳定前缀，长期命中缓存 ────────────┘  └──── 每次变化 ────┘
```

**不要**在提示词里注入时间戳、随机数、每次变化的统计值——那会让整段前缀缓存失效。

---

## 3. 目录结构

```
ai-service/
├── app/
│   ├── main.py               FastAPI 入口 / 异常处理 / lifespan
│   ├── api/                  路由层（health、complete）
│   ├── chains/               编排层
│   │   ├── orchestrator.py     组装 → 裁剪 → 调用 → 计费（唯一知道全流程的地方）
│   │   ├── persona.py          派蒙人格分层组装
│   │   ├── context_render.py   玩家上下文 → 自然语言摘要
│   │   └── routing.py          推理档位路由
│   ├── core/                 settings / logger / envelope / errors / security / tokens
│   ├── llm/                  ChatOpenAI 工厂 + 档位定义
│   ├── schemas/              对外契约模型（Pydantic）
│   └── store/                提示词存储（file / http）+ TTL 缓存
├── prompts/paimon.json       开发默认提示词（生产走 DB）
├── db/migrations/            主服务侧 ai_prompts 建表 + 种子数据
├── tests/
├── Dockerfile                多阶段构建 / 非 root 运行 / 内置健康检查
├── docker-compose.yml        单服务编排，端口只绑回环
├── .dockerignore             确保 .env 不进构建上下文
├── .env.example              配置模板（复制为 .env）
└── pyproject.toml
```

分层纪律：`api → chains → llm/store`，`core` 被各层依赖但不反向依赖。

---

## 4. 配置

见 `.env.example`。几个关键项：

| 变量 | 说明 |
| --- | --- |
| `AI_SERVICE_KEY` | 校验 `X-AI-Key`，**必须独立生成，禁止复用主服务密钥** |
| `DEEPSEEK_API_KEY` | 上游模型密钥，仅走环境变量 |
| `LLM_TIMEOUT_SECONDS` | 默认 55（AI 服务 60s 上限，主服务 65s 兜底） |
| `HISTORY_BUDGET_TOKENS` | 历史裁剪预算，默认 6000 |
| `PROMPT_STORE_MODE` | `file`（开发）/ `http`（生产，调主服务内部接口） |
| `LLM_MAX_CONCURRENCY` | 上游并发上限，防止同步链路被占满 |

---

## 5. 错误码约定

HTTP 状态码 == 响应体 `code`：

| code | 含义 | 主服务是否可重试 |
| --- | --- | --- |
| 400 | 入参不合法 / 超长 | ❌ |
| 401 | `X-AI-Key` 缺失或错误 | ❌ |
| 502 / 503 / 504 | 上游模型错误 / 限流 / 超时 | ✅（重试 ≤ 1 次） |

统一信封，且**永不**向前端或主服务透传上游原始错误与密钥。

---

## 6. 阶段状态

| 阶段 | 状态 |
| --- | --- |
| P0 `/health` + `/v1/complete` + 鉴权 + 超时降级 | ✅ 已实现 |
| P1 人格入库 + token 裁剪 + 档位路由 + 玩家上下文注入 | ✅ 已实现 |
| P2 RAG（Loader → Embeddings → Chroma → 引用来源） | ⏳ 待做（方案见 `doc/02-AI派蒙架构设计-DeepSeek方案.md` §5） |
| P3 LangGraph 工具 + SSE（`/v1/complete/stream` 已实现） | 🚧 流式已通，Agent 待做 |
| P4 用量成本统计（含缓存命中区分）+ 限流熔断 | ⏳ 待做（日志已带 `tokens_cached`） |

> ⚠️ **当前最大的缺口**：主服务的 `remoteAIClient` 尚未接入，所以真实用户还用不上本服务。
> 详细的排期、依赖、决策点与里程碑见 `doc/05-开发路线图.md`。

---

## 7. Docker 部署（可选）

> **本地开发不需要 Docker。** 直接用 §1 的 `python -m app.main` 就行；本节面向部署环境。

镜像与编排文件已就绪，与主服务各自独立运行、只通过 HTTP 契约通信。

```powershell
cd ai-service
Copy-Item .env.example .env      # 首次：填好 AI_SERVICE_KEY / DEEPSEEK_API_KEY

docker compose up -d --build
docker compose ps                # 等状态变成 healthy
docker compose logs -f
docker compose down
```

改了代码要重新构建：`docker compose up -d --build`。

### 几个刻意的设计

| 设计 | 原因 |
| --- | --- |
| compose 里覆盖 `AI_SERVER_HOST=0.0.0.0` | `.env` 里是 `127.0.0.1`，容器内监听回环会导致宿主访问不到，必须覆盖 |
| 端口映射写成 `127.0.0.1:8090:8090` | 只绑宿主回环：主服务能访问，局域网/公网访问不到（AI 服务不得暴露公网） |
| 密钥走 `env_file` 而不是 build args | `.dockerignore` 已排除 `.env`，密钥不会进入镜像层 |
| 非 root 用户（uid 10001） | 降低容器被攻破后的影响面 |
| 健康检查用 `urllib` 而非 `curl` | slim 镜像不带 curl，避免为此多装一个包 |
| 多阶段构建（builder 装依赖 → runtime 只带 venv） | 运行镜像不含编译工具链，体积更小 |
| `extra_hosts: host.docker.internal` | 容器内回调宿主上的主服务时用 `http://host.docker.internal:8080` |

### 主服务怎么连容器里的 AI 服务

主服务与 AI 服务是独立部署，所以 `ai.base_url` 写**宿主地址**，不要写容器名：

```yaml
# 主服务 config.yaml（两者同机时）
ai:
  provider: remote
  base_url: http://127.0.0.1:8090
```

若主服务也在 Docker 里：让两个容器加入同一个自定义网络，然后写 `base_url: http://teyvat-ai:8090`，
并把 compose 里的 `ports` 去掉（同网络内不需要映射宿主端口，更安全）。

### 可选依赖（P2 RAG / P3 LangGraph）

```powershell
docker build --build-arg EXTRAS="[rag,graph]" -t teyvat-ai:0.1.0 .
# 或改 docker-compose.yml 里的 build.args.EXTRAS
```

> 注：`Dockerfile` 尚未在装有 Docker 的环境实测构建（开发机未安装 Docker）。
> 已验证的部分：`docker-compose.yml` YAML 语法正确；`pip install .`（非 editable，与镜像内一致）
> 能完整打包全部 7 个子包。
