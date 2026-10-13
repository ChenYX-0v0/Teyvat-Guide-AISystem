"""服务配置。

pydantic-settings 默认大小写不敏感，字段名直接对应环境变量：
    ai_server_port  <-  AI_SERVER_PORT
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 服务 ----------
    ai_server_host: str = "127.0.0.1"
    ai_server_port: int = 8090
    ai_service_key: str = ""
    allow_insecure_dev: bool = False

    # ---------- 日志 ----------
    ai_log_level: str = "INFO"
    ai_log_json: bool = True

    # ---------- DeepSeek ----------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: float = 55.0
    llm_max_retries: int = Field(default=1, ge=0, le=2)
    llm_max_concurrency: int = Field(default=32, ge=1)
    llm_default_profile: str = "chat"

    # ---------- 上下文预算 ----------
    # 2026-10-12 上调（用户反馈"回答普遍偏短 / 展柜 12 位只答出 7 位"）：
    # 提示词本体（v11）实测 ≈ 1633 token，旧的 system_budget=3000 只留 1367 给动态区 ✗，
    # 而玩家面板自己要 1500 ⇒ 面板被削、依据变少 ⇒ 回答自然短。
    # 2026-10-13 二次上调（接入角色机制卡，见 teyvat-docs/ai/07 TODO-20）：
    # 动态区现在最多有 4 块 —— 玩家面板 3000 + 机制卡 2600（2 张卡 1800 + 规则 800 ✓）
    # + 攻略卡 2400 + 权威资料 1200 ⇒ 峰值 ≈ 9200 ✗，旧的 8000 - 1633 ≈ 6367 装不下，
    # 会出现"机制卡/攻略卡进来把面板挤掉"✗。现在 12000 - 1700(v12 提示词本体) ≈ 10300 ✓
    # ⇒ **任何组合都放得下** ✓（多花的 token 只在这几块真的命中时才发生 ✓，且前缀缓存仍命中 ✓）。
    history_budget_tokens: int = 6000
    system_budget_tokens: int = 12000
    # player_context_budget_tokens 是 render_context 的**总**预算（摘要块 + 角色条目共用 ✓）：
    # 摘要里现在装着机制卡/攻略卡/权威资料 ⇒ 旧值 3000 会让"摘要吃光预算、角色条目被丢"✗。
    player_context_budget_tokens: int = 9000

    # ---------- 入参防护 ----------
    max_prompt_chars: int = 4000
    max_history_messages: int = 40

    # ---------- 事前短路守卫（L2；设计见 teyvat-docs/ai/09，登记见 ai/07 TODO-12） ----------
    # 命中特征（越狱套话 / 现实任务 / 情感类 / 严重违规）→ 直接回固定话术、**不调用大模型**：
    # 零 token 成本、结果确定。rules 那三句话术是概率约束，这里是确定性闸门。
    # off = 不做；observe = 只记日志（灰度观测误伤率）；block = 拦截（默认）
    guard_enabled: bool = True
    guard_mode: str = "block"

    # ---------- 无关问题判定（口径见 shared-docs/05 §3） ----------
    # off = 不判定（默认，off_topic 全为 NULL）
    # sample = 会话首问必判 + 其余按比例抽样（成本与覆盖的折中）
    # all = 每条提问都判（最准，成本最高）
    ai_classify_mode: str = "off"
    ai_classify_sample_rate: float = 0.2

    # ---------- 告警（P4.4；规则口径见 04-部署运维接入说明 §8.3） ----------
    # 进程内评估 + 双通道投递：日志永远有；配了 webhook 再 POST 一份通用 JSON。
    # ⚠️ 覆盖不到"进程整体挂掉"——那种情况进程内发不出任何东西，必须靠外部探针。
    alert_enabled: bool = True
    alert_webhook_url: str = ""
    alert_window_seconds: int = Field(default=300, ge=30, le=86400)  # 统计窗口
    alert_eval_interval_seconds: int = Field(default=30, ge=5, le=3600)  # 评估间隔
    alert_cooldown_seconds: int = Field(default=1800, ge=0, le=86400)  # 同一规则重复通知冷却
    alert_server_error_threshold: int = Field(default=3, ge=1)  # 窗口内 5xx 次数
    alert_5xx_ratio_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    alert_min_requests: int = Field(default=20, ge=1)  # 请求数不足不判占比（防误报）
    alert_cache_min_samples: int = Field(default=20, ge=1)  # 判缓存失效所需最少生成次数

    # ---------- 提示词来源 ----------
    prompt_store_mode: str = "file"  # file | http
    prompt_store_file: str = "prompts/paimon.json"
    prompt_store_url: str = ""
    prompt_store_key: str = ""
    prompt_store_timeout_seconds: float = 3.0
    prompt_store_cache_ttl: int = 60

    # ---------- CORS ----------
    cors_origins: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def validate_runtime(self) -> None:
        """启动前自检：配置错误要 fail fast，不要等到线上才发现。"""
        problems: list[str] = []

        if not self.ai_service_key and not self.allow_insecure_dev:
            problems.append(
                "AI_SERVICE_KEY 为空。必须设置独立密钥（禁止复用主服务密钥）；"
                "本地临时调试可显式设置 ALLOW_INSECURE_DEV=true。"
            )

        if not self.deepseek_api_key:
            problems.append("DEEPSEEK_API_KEY 为空，无法调用上游模型。")

        if self.prompt_store_mode == "http" and not self.prompt_store_url:
            problems.append("PROMPT_STORE_MODE=http 时必须提供 PROMPT_STORE_URL。")

        if self.prompt_store_mode not in {"file", "http"}:
            problems.append(
                f"PROMPT_STORE_MODE 非法：{self.prompt_store_mode}（仅支持 file / http）。"
            )

        if self.ai_classify_mode not in {"off", "sample", "all"}:
            problems.append(
                f"AI_CLASSIFY_MODE 非法：{self.ai_classify_mode}（仅支持 off / sample / all）。"
            )

        if not 0.0 <= self.ai_classify_sample_rate <= 1.0:
            problems.append("AI_CLASSIFY_SAMPLE_RATE 必须在 0~1 之间。")

        if self.alert_eval_interval_seconds > self.alert_window_seconds:
            problems.append(
                "告警配置不合理：ALERT_EVAL_INTERVAL_SECONDS 不应大于 ALERT_WINDOW_SECONDS"
                "（否则窗口内只够评估一次）。"
            )

        if problems:
            raise RuntimeError("配置校验未通过：\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
