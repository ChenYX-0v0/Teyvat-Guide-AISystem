# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.14

# ============================================================
# builder —— 只负责把依赖装进独立的 venv
# ============================================================
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# 先单独拷贝 pyproject：依赖不变时这一层能命中构建缓存
COPY pyproject.toml ./
COPY app ./app

# P2/P3 需要额外依赖时，把 EXTRAS 设为 "[rag]" / "[graph]" / "[rag,graph]"
ARG EXTRAS=""
RUN python -m pip install --upgrade pip \
    && python -m pip install ".${EXTRAS}"

# ============================================================
# runtime —— 最小运行时，非 root 用户
# ============================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    AI_SERVER_HOST=0.0.0.0 \
    AI_SERVER_PORT=8090 \
    PROMPT_STORE_FILE=/app/prompts/paimon.json

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
COPY prompts ./prompts
COPY db ./db

# 非 root 运行；容器被攻破时影响面更小
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 paimon \
    && chown -R paimon:paimon /app
USER paimon

EXPOSE 8090

# 用 python 自带的 urllib 做探活，镜像里不需要装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.getenv('AI_SERVER_PORT','8090')+'/health',timeout=4).getcode()==200 else 1)"]

# 唯一入口：与本地开发一致（会读取环境变量里的 host/port）
CMD ["python", "-m", "app.main"]
