# ============================================================
# Dockerfile - 游乐场RAG智能知识库问答系统
# 多阶段构建：减少镜像体积
# ============================================================
FROM python:3.10-slim

LABEL maintainer="amusement-park-rag"
LABEL description="Multi-Agent RAG System for Amusement Park"

WORKDIR /app

# ---------- 系统依赖 ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---------- 安装 Python 依赖（利用Docker缓存） ----------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && rm -rf /root/.cache/pip

# ---------- 预下载 HuggingFace 模型（可选，减少首次启动时间） ----------
# RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"

# ---------- 复制应用代码 ----------
COPY . .

# ---------- 创建必要目录 ----------
RUN mkdir -p /app/docs /app/logs /app/models_cache

# ---------- 暴露端口 ----------
EXPOSE 8000

# ---------- 健康检查 ----------
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ---------- 启动命令 ----------
CMD ["python", "main.py"]
