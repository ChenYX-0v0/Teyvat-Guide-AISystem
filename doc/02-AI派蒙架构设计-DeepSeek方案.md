# AI 派蒙架构设计（LangChain + DeepSeek）

> **接口的权威表述见 `01-接口契约.md`**；本文讲设计取舍与实现。
>
> 本文为 **v2**，决策要点：AI 服务采用 **Python + LangChain**（原 v1 的 Go 方案作废）。
> 一句话：**主服务 Go :8080 管业务与持久化不变，AI 派蒙服务用 Python/LangChain 做 :8090，DeepSeek 通过官方集成接入；对外 HTTP 契约保持稳定（见 §3 与 README）。**

---

## 0. 关键决策与偏差登记（必读）

| 项 | v1（Go） | v2（LangChain，本方案） |
| --- | --- | --- |
| AI 服务语言 | Go | **Python 3.11+ / FastAPI** |
| 编排方式 | 手写流水线 | **LangChain LCEL + LangGraph（后续）** |
| 复用主服务 `pkg/*` | 可以 | **不可以**，自建 logger / 响应信封（见下） |
| 对外契约 | `/health` `/v1/complete` | **完全一致，逐字不变** |
| 主服务改造 | `provider=remote` | **完全一致**，仍是 `remoteAIClient` 一条链路 |

**偏差说明（需要你在 `AGENTS.md` / `docs/01` 登记）**：
原方案建议"同仓可复用 `pkg/logger`、`pkg/response`"。跨语言后该建议不可行，替代方案：
1. **响应信封在 Python 侧重新实现**，字段与 Go 版逐字节对齐（`{code,message,data}`），用 Pydantic model 保证结构；
2. **日志用 structlog 输出 JSON 行**，字段命名对齐 `logger.L()`（`level/ts/msg/...`），便于统一采集；
3. 代码仍放同仓 `ai-service/` 目录（独立进程），靠契约而非代码耦合。

> 这样做的收益：LangChain 生态全量可用、学习目标达成、未来工具/Agent 扩展顺畅。
> 代价：多一套 Python 运行时与依赖管理，运维面变宽（见 §10 风险）。

---

## 1. 总体架构

```
  React SPA ─/api─► teyvat-guide 主服务 (Go, :8080)          ← 零改动，除 ai.provider=remote
                    │  ChatService：落库 → 取20条历史 → AIClient
                    │  remoteAIClient（HTTP + X-AI-Key）
                    ▼
        AI 派蒙服务 (Python 3.11 + FastAPI + LangChain, :8090)
        ┌───────────────────────────────────────────────────────┐
        │ api/        /health  /v1/complete  /v1/complete/stream │
        │ chains/     编排层                                      │
        │   ├ persona.py         派蒙人格分层（提示词从 DB 读）    │
        │   ├ routing.py         推理档位路由（thinking 开关）     │
        │   ├ context_render.py  玩家数据 → 自然语言摘要           │
        │   ├ orchestrator.py    组装→裁剪→调用→计费              │
        │   └ graph/             LangGraph 状态机 (P3+ 工具/Agent) │
        │ llm/        ChatOpenAI 指向 DeepSeek 兼容端点            │
        │ store/      prompt_store(只读 ai_prompts) / vector(P2) │
        │ core/       settings / logger / envelope / security    │
        └──────────────────┬────────────────────┬───────────────┘
                           ▼                    ▼
              DeepSeek API                Embedding 服务 (P2)
        deepseek-flash（单模型，是否推理     bge-m3 本地 或 API
        由 thinking 参数控制）
        （远期）LangSmith 追踪（可选）
```

---

## 2. LangChain 技术选型

```
# pyproject.toml 核心（P0/P1 实装）
fastapi / uvicorn[standard]          # HTTP 层
pydantic / pydantic-settings         # 信封与配置模型
langchain-core                       # Runnable / trim_messages / 消息类型
langchain-openai                     # ChatOpenAI（指向 DeepSeek 兼容端点）
structlog                            # JSON 日志
httpx                                # 调主服务内部接口
# 按阶段安装（optional-dependencies）
langchain-community / chromadb / sentence-transformers   # P2 RAG
langgraph                                                # P3 状态机与工具
```

**DeepSeek 接入方式（已实装）**：用 `langchain_openai.ChatOpenAI` 指向 `https://api.deepseek.com`，专有参数通过 `extra_body` 透传：

```python
ChatOpenAI(
    model="deepseek-flash",
    base_url="https://api.deepseek.com",
    api_key=...,
    extra_body={"thinking": {"type": "disabled"}},   # 或 enabled + reasoning_effort
    stream_usage=True,                                # 流式拿 token 统计必需
)
```

为什么不用 `langchain-deepseek`：新模型 `deepseek-flash` 发布即要能用上，
而社区集成包的模型枚举与参数支持往往滞后；`ChatOpenAI` 是 OpenAI 兼容协议的标准通道，
换供应商/换模型只改 `llm/deepseek.py` 一个文件。

> 实装环境为 **langchain-core 1.6.x / langchain-openai 1.6.x**（LangChain v1 线）。
> 注意 v1 中 `ChatOpenAI` 的构造参数不再出现在 `__init__` 签名上，
> 因此参数探测需同时读 `model_fields` 及其别名字段（代码已处理）。

**明确不用的 LangChain 能力**（违反服务边界）：
- ❌ `ConversationBufferMemory` / `ChatMessageHistory` 等 Memory 类 —— AI 服务必须**无状态**，会话持久化归主服务
- ❌ `ChatPromptTemplate.from_template` 里硬编码提示词 —— 违反"业务数据入库"强制规范
- ❌ LangChain 自带 retry 叠加自己写的 retry —— 重试 ≤1 次，总耗时有上限

---

## 3. 核心链路（实现 `POST /v1/complete`）

```python
# chains/orchestrator.py —— 对应"组装 → 裁剪 → 路由 → 调用"
# 实装为显式方法链（便于单测与逐步调试），框架能力用在刀刃上：
#   · trim_messages / count_tokens_approximately  → 历史裁剪
#   · 消息类型 SystemMessage/HumanMessage/AIMessage → 上下文构造
#   · ChatOpenAI                                   → 模型调用与重试
async def complete(self, req) -> CompleteData:
    self._validate(req)                                    # 入参防护
    profile = decide_profile(req, default_profile)         # ① 推理档位路由
    llm = self._build_llm(profile, req.options, streaming=False)   # ② 错误边界
    messages, system_text = await self._build_messages(req)        # ③ 人格+上下文+裁剪
    async with self._sem:                                  # ④ 并发闸门
        message = await llm.ainvoke(messages)              # ⑤ 上游调用
    return to_envelope(message, profile, elapsed_ms)       # ⑥ 信封映射
```

历史裁剪用框架能力替代手写估算：

```python
trimmed = trim_messages(
    messages,
    strategy="last", token_counter=count_tokens_approximately,
    max_tokens=settings.history_budget_tokens,   # 默认 6000
    include_system=False, allow_partial=False, start_on="human",
)
```

调用侧超时与重试（约定 60s 上限 / 重试 ≤1）：

```python
ChatOpenAI(model="deepseek-flash", timeout=55, max_retries=1, stream_usage=True)
# max_retries 即 OpenAI SDK 的退避重试；429/503 自动退避，4xx 不重试
```

> **与纯 LCEL 的差异（有意为之）**：本链路含并发闸门、错误分类、用量统计、双模式（同步/流式）分支，
> 用 `RunnableBranch` 管道表达反而不如显式代码可读、可测。LangChain 的组件（trim_messages、
> 消息类型、ChatOpenAI、P2 的 Retriever/VectorStore、P3 的 LangGraph）照常使用，
> 只是不为了"用框架"而把控制流硬塞进管道。

**DeepSeek 前缀缓存友好原则**：人格提示词、few-shot 示例逐字节稳定，动态内容（RAG 资料、玩家档案、用户提问）一律放消息序列尾部。

---

## 4. 派蒙人格（提示词入库，LangChain 只做渲染）

```
ai_prompts 表（主服务库，AI 服务只读 + TTL 60s 内存缓存）
  scene_key | content | version | enabled
  paimon_base | "你是派蒙……" | 3 | 1

渲染顺序（稳定 → 动态）:
  ① persona(DB, 场景=paimon_base)
  ② biz_rules(DB)     拒绝换角色 / 防注入 / 数值不幻觉
  ③ few_shot(DB)      2~3 组派蒙语气示例
  ④ rag_context       检索结果 + 来源标注 (P2)
  ⑤ player_profile    玩家档案摘要 (P1)
```

实装采用**显式拼装**：稳定三段从 DB 取出后按序拼接，动态段按 token 预算逐段填充、超预算即丢弃，
最终生成单条 `SystemMessage`：

```python
messages = [SystemMessage(content=system_text)] + trimmed_history + [HumanMessage(content=prompt)]
```

为什么不用 `ChatPromptTemplate.from_messages`：内容来自 DB、且动态段需要**按预算选择性裁剪**
（RAG 资料可丢、玩家档案可截断），模板引擎在这里不增加价值反而挡住控制流。
`ChatPromptTemplate` 留给 P2 的 RAG 子链使用。

---

## 5. RAG 设计（P2，LangChain 主战场）

```
离线索引:
  asset_characters / asset_weapons / asset_relic_sets
  asset_character_stat_weights / asset_text_map_entries / 攻略文本
        │  DocumentLoader（导出 JSON/CSV → Document）
        ▼
  RecursiveCharacterTextSplitter(chunk=500, overlap=80, 中文分隔符)
        ▼
  Embeddings: bge-m3（本地 sentence-transformers 或 API）
        ▼
  Chroma（开发）/ Milvus 或 pgvector（生产）

在线检索:
  route → retriever(MMR, top_k=5, score_threshold) → 拼入 system 尾部
       → 回答要求标注来源 ["来源: asset_character_stat_weights#胡桃"]
```

要点：
- **Embedding 不能用 DeepSeek**（不提供该 API），必须另配；本地 bge-m3 免费用但要 GPU/内存，API 方案（SiliconFlow 等）省运维
- 检索命中片段**只作为资料**注入，人格层强制"资料外不编造"
- 用 `RunnableParallel` 并行做检索与档案拉取，压缩首字延迟

---

## 6. LangGraph：为"将来接入很多功能"预留

P3 起把线性 LCEL 升级为状态机，工具型需求都挂这里：

```
StateGraph(PaimonState)
  route ──┬─► chat ─────────────► END
          ├─► rag_search ────────► generate ─► END
          └─► tool_call ──┬─► get_player_profile   (httpx 调主服务 /api/genshin/player)
                          ├─► lookup_character      (资产库查询)
                          └─► calc_stats            (伤害/词条计算)
                                   │
                                   └─► 可循环（ReAct 风格，带最大步数上限）
```

- 工具即 @tool 函数，LangChain 的 `bind_tools` 直接支持 DeepSeek function calling
- ⚠️ **工具节点必须走 `tool` 档位（thinking=disabled）**：DeepSeek 在思考模式下不支持
  `tool_choice=required` 与指定具名工具，会直接返回 400
- 加 `max_iterations` 与总超时，防止 Agent 循环烧钱（禁止"长超时+重试叠加"）
- 优势：以后加"派蒙帮我看深渊阵容"这类多步能力，只加节点不改契约

---

## 7. 目录结构

```
ai-service/                          # 独立项目/仓库，独立进程
├── pyproject.toml
├── Dockerfile / docker-compose.yml / .dockerignore
├── doc/                          项目文档 01~06（契约/架构/对接/部署/路线图/阿里云）
├── app/
│   ├── main.py                      # FastAPI 入口
│   ├── api/
│   │   ├── health.py                # GET /health
│   │   └── complete.py              # POST /v1/complete (+ /stream)
│   ├── core/
│   │   ├── settings.py              # pydantic-settings + 启动自检
│   │   ├── logger.py                # structlog JSON（含 zap 风格 *w 别名）
│   │   ├── envelope.py              # {code,message,data} 对齐 Go 版
│   │   ├── errors.py                # 4xx 不可重试 / 5xx 可重试 分类
│   │   ├── security.py              # X-AI-Key 依赖（常量时间比较）
│   │   └── tokens.py                # token 估算与预算裁剪
│   ├── schemas/
│   │   ├── chat.py                  # 对外契约模型
│   │   └── context.py               # 可选扩展：玩家上下文
│   ├── chains/
│   │   ├── orchestrator.py          # 组装 → 裁剪 → 调用 → 计费
│   │   ├── persona.py               # 人格分层组装
│   │   ├── context_render.py        # 玩家数据 → 自然语言摘要
│   │   ├── routing.py               # 推理档位路由
│   │   ├── rag.py                   # P2
│   │   └── graph/paimon_graph.py    # P3
│   ├── llm/
│   │   ├── deepseek.py              # ChatOpenAI 工厂（超时/重试/参数探测）
│   │   └── profiles.py              # 推理档位定义
│   ├── store/
│   │   ├── prompt_store.py          # 只读 ai_prompts（TTL 缓存）
│   │   ├── file_store.py            # 开发后端
│   │   ├── http_store.py            # 生产后端（调主服务内部接口）
│   │   └── vector_store.py          # P2
│   └── tools/                       # P3 Agent 工具
└── tests/
```

**`prompt_store` 取数方式**（实装支持 A / 开发用 file 兜底）：
- A. **httpx 调主服务内部接口**（`PROMPT_STORE_MODE=http`）→ 严格守住"AI 服务不碰业务库"的边界，生产推荐；需主服务加只读内部接口，用 `X-Internal-Key` 鉴权
- B. 本地 JSON（`PROMPT_STORE_MODE=file`）→ 开发默认，不依赖主服务即可跑通 P0
- ❌ 直连 MySQL → 排除。违反"AI 服务不落业务库"的精神，且 schema 变更会跨服务断裂

---

## 8. 配置

```yaml
# ai-service/.env（与实装 .env.example 一致）
AI_SERVER_PORT=8090
AI_SERVICE_KEY=                  # 独立生成，禁止复用主服务密钥
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash         # 官方唯一模型 ID
LLM_TIMEOUT_SECONDS=55
LLM_MAX_RETRIES=1                # 重试 ≤1（硬约束）
LLM_MAX_CONCURRENCY=32
LLM_DEFAULT_PROFILE=chat         # chat | analysis | tool | json
HISTORY_BUDGET_TOKENS=6000
SYSTEM_BUDGET_TOKENS=3000
PLAYER_CONTEXT_BUDGET_TOKENS=1500
PROMPT_STORE_MODE=file|http
PROMPT_STORE_URL=http://127.0.0.1:8080
PROMPT_STORE_KEY=                # mode=http 时的 X-Internal-Key
# P2
EMBEDDING_PROVIDER=local|api
VECTOR_STORE=chroma
```

主服务侧仍是三行改动：`ai.provider=remote`、`ai.base_url=http://127.0.0.1:8090`、`ai.api_key=$AI_SERVICE_KEY`（外加 `timeout_seconds` 与降级兜底）。

---

## 9. 阶段计划（LangChain 学习路径 + 交付并行）

> 本节是**学习视角**的阶段表。执行视角的排期、依赖、决策点与里程碑见 `05-开发路线图.md`。

| 阶段 | 交付 | 你会学到的 LangChain 概念 |
| --- | --- | --- |
| **P0** | FastAPI + `/health` + `/v1/complete`，`ChatOpenAI` 直连 DeepSeek，`X-AI-Key`，超时/错误分类/降级 | `ChatModel` 接口、`ainvoke`、消息类型（System/Human/AI） |
| **P1** | 提示词来源抽象 + 派蒙人格分层 + `trim_messages` 裁剪 + 推理档位路由 + 玩家上下文注入 | `trim_messages`、`count_tokens_approximately`、结构化输出模型 |
| **P2** | RAG：Loader → Splitter → Embeddings → Chroma → Retriever → 引用标注 | `Document`、`TextSplitter`、`Embeddings`、`VectorStore`、`Retriever`、RAG Chain |
| **P3** | LangGraph 状态机 + 工具调用 + SSE 流式（`astream`） | `StateGraph`、`@tool`、`bind_tools`、`astream_events`、流式事件映射 |
| **P4** | 用量/成本统计（含 DeepSeek 缓存命中区分）、限流熔断、LangSmith 可选追踪 | `callbacks`、`RunnableConfig`、`CallbackHandler` |

---

## 10. 风险与对策

| 风险 | 对策 |
| --- | --- |
| Python 运行时运维面变宽（与原 Go 单栈背离） | 固定版本 + `uv` 锁定依赖 + Dockerfile；部署与主服务同机不同容器 |
| LangChain 版本迭代快、破坏性变更 | 锁版本（不追 latest）；**所有框架调用集中在 `chains/` 与 `llm/`，换版本只改这一层**；保留 `ChatOpenAI(base_url=deepseek)` 兜底 |
| 抽象层遮挡 DeepSeek 特性（缓存/`reasoning_content`） | 人格分层保证前缀稳定；专有参数走 `extra_body` 透传；`reasoning_content` 从 `additional_kwargs` 读取，必要时下钻 `llm._client` |
| LangChain v1 破坏性变更（`ChatOpenAI` 参数不在 `__init__` 签名上） | 参数探测同时读 `model_fields` 与别名字段；探测失败时**不过滤**（fail open），绝不丢 `model` / `api_key` |
| Agent 循环烧钱/超时 | LangGraph 硬性 `max_iterations`，总超时 60s，与主服务 65s 兜底对齐 |
| Embedding 与 LLM 双供应商密钥管理 | 全部走环境变量；`.env.example` 只留空值 |
| 与原方案"复用 pkg/*"的偏差 | 已在 §0 登记，用"契约对齐 + 结构化日志字段对齐"消解，需同步更新 `AGENTS.md` |

---

## 11. 不变的硬约束（自检清单）

- [ ] 对外接口 `/health`、`/v1/complete`、`/v1/complete/stream` 的路径、字段、信封保持逐字稳定
- [ ] `history` 仅 `user`/`assistant`；`system` 独立字段，不混入
- [ ] 无鉴权不得暴露 `/v1/*`；不向公网暴露 8090
- [ ] 提示词、模型清单、限流阈值全部入表/配置，无硬编码
- [ ] 密钥仅环境变量，日志不打印 Key 与 LLM 原始错误
- [ ] AI 服务无状态：不建用户体系、不写 `chat_*` 表
- [ ] 重试 ≤1 次且总耗时有上限（不得叠加长超时）

---

## 12. 项目完全独立下的"用户养成数据分析"方案

**结论：「完全独立」= 代码独立 + 部署独立 + 存储独立，但数据允许单向流动。独立性 ≠ 隔离。**

### 12.1 数据分三类，处理方式不同

| 数据类 | 例子 | 所有者 | 给 AI 服务的方式 |
| --- | --- | --- | --- |
| 用户私有数据 | 拥有角色、等级/命座/天赋、圣遗物、武器 | 主服务 | **随请求推送（push）**，AI 服务不落库 |
| 公共资产字典 | `asset_characters` / `stat_weights` / 攻略文本 | 主服务（只读非私有） | **允许 ETL 镜像**到 AI 服务自己的向量库 |
| 配置类 | `ai_prompts` | 主服务 | 内部接口拉取 + TTL 缓存 |

红线：用户私有数据**不得**被 AI 服务持久化；公共知识**可以**镜像（非用户数据，且 RAG 需要本地索引）。

### 12.2 三种集成模式

**模式 A：上下文注入（Push）— P1 优先，覆盖大多数场景**

主服务把玩家数据压缩为结构化 JSON 放入请求（`POST /v1/complete` 的**可选扩展字段**，向后兼容）：

```json
{
  "system": "...",
  "history": [ ... ],
  "context": {
    "player": {
      "uid": "...", "level": 58,
      "characters": [{
        "name": "胡桃", "level": 90, "constellation": 1,
        "talents": { "normal": 9, "skill": 9, "burst": 8 },
        "weapon": { "name": "护摩之杖", "refine": 1, "level": 90 },
        "artifacts": [{ "set": "炽烈的炎之魔女", "count": 4, "mainStats": {} }]
      }]
    },
    "goal": "深境螺旋 12 层"
  },
  "prompt": "帮我看看我这队怎么提升？"
}
```

- AI 服务将 `context.player` 渲染为自然语言摘要，拼入 **system 尾部**（动态区，不影响 DeepSeek 前缀缓存）
- 优点：AI 服务真无状态；主服务掌握数据出口（裁剪/脱敏/控量）；无需给 AI 服务开任何 DB 账号
- 主服务侧实现：`ChatService` 内复用 `PlayerService` 取档案 → 生成摘要 → 由 `remoteAIClient` 透传

**模式 B：内部只读 API（Pull）— 供 LangGraph 工具调用**

AI 需要自主决定取数时（如"分析我所有 90 级角色"），主服务新增内部接口：

```
GET /internal/v1/player/{uid}/profile
GET /internal/v1/player/{uid}/characters?minLevel=90
GET /internal/v1/asset/characters/{name}
鉴权：X-Internal-Key（独立于用户 JWT 与 X-Admin-Key）；仅内网/回环可达，不暴露公网
```

AI 服务侧实现为 LangGraph `@tool`（httpx 调用），仍不碰 MySQL；必须配限流与单次拉取条数上限。

**模式 C：数据镜像（ETL）— 供 RAG 使用**

```
GET /internal/v1/asset/export?type=characters&since=<updated_at>
```

AI 服务定时增量同步至自己的向量库/PostgreSQL，作为 RAG 知识基座；用 `updated_at` + 版本号消除一致性窗口。

### 12.3 底线（违反即失去独立性）

- ❌ AI 服务直连主服务 MySQL（哪怕只读账号）——schema 变更将跨服务断裂，最隐蔽的反模式
- ❌ 用户数据落进 AI 服务的库（破坏无状态，且有隐私合规风险）
- ❌ 跨项目 import 代码 / 共享 ORM model
- ✅ 唯一耦合面：`/v1/complete`（+ 可选 `context` 字段）与 `/internal/*` 两组契约

### 12.4 落地节奏

| 阶段 | 模式 | 达成效果 |
| --- | --- | --- |
| P1 | A（push） | "根据我现有角色给建议"直接可用 |
| P2 | C（资产镜像） + RAG | 词条/攻略知识本地检索，含引用来源 |
| P3 | B（内部 API + LangGraph 工具） | Agent 自主取数、多步分析（阵容/深渊规划） |

数值分析类问题（配队、词条收益、深渊阵容）自动路由至 `analysis` 档位（`thinking=enabled`）。
无需主服务指定；关键词命中即升档，且**不额外消耗模型切换成本**（本来就是同一个 `deepseek-flash`）。

### 12.5 上下文控量（必须做）

- 主服务**先摘要后推送**：按用户提问意图筛选相关角色或仅推用户指定角色/当前出战队伍，**禁止推送全图鉴**
- 单角色压缩为精简字段；圣遗物完整明细（5件×4词条）按需由模式 B 拉取
- 玩家上下文 token 预算 ≤ 1500；超限时降级为"角色列表 + 练度概览"

### 12.6 仓库与契约管理（完全独立形态）

- 两个独立 git 仓库：`teyvat-guide`（Go）与 `teyvat-ai`（Python）
- **契约即边界**：`/v1/complete`（含 `context` 扩展）与 `/internal/*` 抽取为独立 `contracts/`（OpenAPI 3 定义），两侧各自生成客户端
- 契约演进走版本号（`v1`），新增字段一律可选以保持向后兼容
- AI 服务地址、`X-Internal-Key`、`X-AI-Key` 全部环境变量注入

