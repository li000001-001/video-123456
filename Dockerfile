# 免费云部署镜像（Hugging Face Spaces / 任意 Docker 环境）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

# ffmpeg 用于合成；fonts-noto-cjk 保证中文字幕正常显示
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-cjk \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# 字幕组件单独一层，构建失败不影响主功能
COPY requirements-subtitle.txt ./
RUN pip install -r requirements-subtitle.txt || echo "字幕组件安装失败，将使用云端端点"

COPY . .

RUN mkdir -p data && chmod -R 777 /app

EXPOSE 7860

CMD ["python", "app.py"]
