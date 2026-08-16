# Agent Service 生产镜像
# 构建: docker build -t agent-service .
# 多阶段构建: 精简运行镜像体积

# ---- 依赖阶段: 安装全部 Python 依赖 ----
FROM python:3.13-slim AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/agent-service

# 系统依赖: libgomp 供 faiss(OpenMP)使用; 其余常见基础库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 先拷贝依赖清单,利用构建缓存(依赖层在源码变化时不用重装)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 运行阶段: 仅拷贝代码与依赖 ----
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8080 \
    AGENT_ENV_FILE=/srv/agent-service/.env

WORKDIR /srv/agent-service

# faiss 需要 libgomp; 一并装好常用调试工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 从 deps 阶段拷贝 site-packages
COPY --from=deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# 拷贝应用代码
COPY app ./app
COPY .env.example ./

# 数据目录(持久化: docker-compose 挂载 volume)
RUN mkdir -p /srv/agent-service/data && chmod 777 /srv/agent-service/data

EXPOSE 8080

# 健康检查: 依赖 curl(内置镜像无 curl,改用 python)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
