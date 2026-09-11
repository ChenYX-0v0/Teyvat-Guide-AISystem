# AGENTS.md — AI 协作开发规范（teyvat-ai · AI 派蒙服务）

> 本文件面向 AI 编码代理与人类协作者。修改任何代码前先读本文；与本文冲突的旧代码不代表规范，遵循本文。

## 0. 文档入口（共享文档仓）

本项目文档**不在本仓**，而在三项目共享文档仓 `teyvat-docs`（远程 `git@github.com:ChenYX-0v0/Teyvat-Guide-Docs.git`），通过 **Junction 目录链接**挂载。三个项目访问到的文档是**同一份文件**（哈希一致），改一处三处同步。

### 0.1 布局与挂载关系

| 项目 | 项目内路径 | 实际位置 | 内容 |
| --- | --- | --- | --- |
| AI 派蒙服务（本仓） | `doc/` | `teyvat-docs\ai` | 本项目文档（架构设计 / 部署运维 / 路线图 / 阿里云方案 / 文档索引） |
| AI 派蒙服务（本仓） | `shared-docs/` | `teyvat-docs\shared` | **跨项目契约**（01 接口契约 / 03 主服务对接说明 / 04 用户体系与鉴权边界设计） |
| 主服务 teyvat-guide | `docs/`、`shared-docs/` | `teyvat-docs\web`、`\shared` | 主服务文档、共用契约 |
| 后台 demo-app | `docs/`、`aidoc/` | `teyvat-docs\admin`、`\ai` | 后台文档、AI 文档（同源） |

编号只在各自目录内唯一（`ai/02` 与 `web/02` 互不冲突），**交叉引用必须写明路径**（如 `shared-docs/01-接口契约.md`）。

### 0.2 读 / 改 / 提交（标准操作）

```powershell
# 读：任一项目的链接路径都能打开，内容同源，无需特殊操作
# 改：建议在共享仓内编辑（从项目 doc/ 路径编辑也完全等效——同一份文件）

cd C:\Users\HONOR\Desktop\teyvat-docs
git add -A
git commit -m "docs: 说明本次改动"
git push
```

- 提交**只在 `teyvat-docs` 发生**，三个项目仓不产生文档改动。

### 0.3 规矩（必须遵守）

1. 文档**只在共享仓 `C:\Users\HONOR\Desktop\teyvat-docs` 修改并提交**；`doc/`、`shared-docs/` 是链接，禁止在本仓提交文档（已在 `.gitignore` 忽略）。
2. 禁止在本项目内另建文档副本。
3. 改 `shared-docs/01-接口契约.md` 属于**破坏性契约变更**：必须同步检查主服务（teyvat-guide）与后台（demo-app）的调用方，并在提交信息中说明影响面与兼容性。
4. Junction 不进 git：新机器 / 新克隆后运行 `teyvat-docs\setup-docs.ps1` 重建链接（7 个链接）。
5. **本仓 `git status` 必须看不到任何 `doc/`、`shared-docs/` 项**——出现即为 `.gitignore` 忽略规则被破坏（见第 7 条），这是最快的一致性自检信号。
6. 禁止在本仓执行 `git clean -fd`，禁止直接删除 `doc/`、`shared-docs/` 目录——Junction 删除会**连带清空真源内容**，等于删掉共享文档。
7. 禁止删除 `.gitignore` 中的 `/doc/`、`/shared-docs/` 忽略规则，否则 git 会顺着链接把共享文档当项目文件收录。
8. `setup-docs.ps1` 必须存为 **UTF-8 with BOM**（PowerShell 5.1 否则按 GBK 读，中文路径报 `Unexpected token`）；`.env` / `.yaml` 类配置反之不能带 BOM。

> 契约唯一权威表述是 `shared-docs/01-接口契约.md`；契约与代码冲突时以**代码为准并回来修文档**。
> 索引：`doc/README.md`（含按场景跳转与维护约定）。

## 1. 项目定位

基于 **LangChain + DeepSeek-V4.1-Flash** 的独立 AI 服务，为 `teyvat-guide` 主服务提供模型调用能力。

- 对外接口：`GET /health`、`POST /v1/complete`、`POST /v1/complete/stream`（SSE）
- 技术栈：Python + FastAPI（见 `pyproject.toml`），依赖用 `.venv` 隔离
- 启动 / 测试：见 `README.md`（安装、启动、`.env` 配置项、错误码速查）

## 2. 硬约束（违反即事故）

| 约束 | 说明 |
| --- | --- |
| **无状态** | 不写业务库、不管会话存储、不建用户体系；重启即恢复，可水平复制 |
| **三密钥互相独立** | `X-AI-Key`（主服务↔本服务）／`X-Internal-Key`（后台↔主服务）／`X-Admin-Key`（资产管理）**禁止复用**；`.env` 不入库 |
| **不持有用户数据** | 主服务下发的 `context.player`（玩家档案）只在本次请求内存中存在，禁止落盘/落库/写日志 |
| **schema 强校验** | `/v1/complete` 的 `context` 结构按契约校验（已踩坑：`characters[].weapon` 必须是对象、字段名为 `refine`、`artifacts[].mainStats` 的 key 为中文部位名） |
| **错误不泄漏** | 对外统一信封 `{code,message,data}`；不把上游模型原始错误、密钥、内部堆栈透出 |
| **日志** | 禁止打印完整 prompt / 玩家数据 / 密钥；token 用量可记 |

## 3. 通用规范

- 仅改业务代码时不要触碰 `doc/`、`shared-docs/`（属共享仓）。
- 改接口字段 / 错误码 / SSE 协议 → 必须同步 `shared-docs/01-接口契约.md` 与 `README.md`，并通知主服务侧。
- 改部署方式 / 环境变量 → 同步 `README.md`、`.env.example`。
- 改人格提示词结构 → 同步 `shared-docs/` 相关说明与 `prompts/`。
- 新增文档 → 放在共享仓 `ai/` 并更新 `doc/README.md` 索引。
- 提交前：`pytest` 通过（见 `tests/`）、`ruff` 无告警。
- 文档改动只在 `teyvat-docs` 提交，本仓 `git status` 无 `doc/` / `shared-docs/` 项（提交前自检）。
