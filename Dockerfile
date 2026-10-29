FROM python:3.11-slim

WORKDIR /app

# 默认 PyPI；国内构建可传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

RUN useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser . .

RUN mkdir -p data reports && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status == 200 else 1)"
# 滚动发布时给在途 SSE 研究留出收尾时间；Render 免费层不支持 maxShutdownDelaySeconds
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-graceful-shutdown 120"]
