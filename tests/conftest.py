"""测试环境准备。

必须在导入 app 之前设置环境变量（环境变量优先级高于 .env）。
测试不会真实调用 DeepSeek（LLM 一律被 mock）。
"""

import os

os.environ["AI_SERVICE_KEY"] = "test-key"
os.environ["ALLOW_INSECURE_DEV"] = "false"
os.environ["DEEPSEEK_API_KEY"] = "test-deepseek-key"
os.environ["PROMPT_STORE_MODE"] = "file"
os.environ["PROMPT_STORE_FILE"] = "prompts/paimon.json"
os.environ["AI_LOG_JSON"] = "false"
os.environ["AI_LOG_LEVEL"] = "WARNING"
