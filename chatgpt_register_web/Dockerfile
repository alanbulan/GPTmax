FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    http_proxy= \
    https_proxy= \
    all_proxy= \
    HOST=0.0.0.0 \
    PORT=20080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install \
    'requests>=2.32.5' \
    'fastapi>=0.115.0' \
    'uvicorn[standard]>=0.30.0' \
    'curl-cffi>=0.7.0' \
    'python-multipart>=0.0.9'

COPY chatgpt_register.py register.py web_app.py config.json ./
COPY templates ./templates

EXPOSE 20080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:20080/healthz || exit 1

CMD ["python3", "web_app.py"]
