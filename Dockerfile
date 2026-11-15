FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/watcher
COPY pyproject.toml ./
COPY app ./app
# Git checkout under umask 077 can produce root-only source files.
# The runtime runs as UID 10001 and must be able to read Python modules.
RUN chmod -R a+rX /opt/watcher/app
RUN pip install --no-cache-dir .
RUN mkdir -p /data && useradd -u 10001 -m watcher && chown watcher:watcher /data
USER watcher
CMD ["python", "-m", "app.bot"]
