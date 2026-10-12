FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GOLDEN_PIT_DATA=/data GOLDEN_PIT_HOST=0.0.0.0 GOLDEN_PIT_PORT=5000

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY golden_pit ./golden_pit
RUN pip install --no-cache-dir ".[server,mcp]" \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /data && chown app /data

USER app
VOLUME ["/data"]
EXPOSE 5000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health')"

# 扫描状态与调度器在进程内：只能 1 个 worker（多线程即可）
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "120", "-b", "0.0.0.0:5000", "golden_pit.server:create_app_from_env()"]
