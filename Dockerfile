FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/watcher
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
RUN mkdir -p /data && useradd -u 10001 -m watcher && chown watcher:watcher /data
USER watcher
CMD ["python", "-m", "app.bot"]
