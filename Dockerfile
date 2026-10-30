# The Matrix integration: an application service for the homeserver and the
# service API the capture service polls. No media passes through it.
#
#   docker build -t voxonta-matrix-app .
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && useradd --system --no-create-home voxonta

USER voxonta
# 8080: transactions from the homeserver — keep it inside the homeserver's
# network. 8081: the service API for the capture service.
EXPOSE 8080 8081 8082
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=4)"
CMD ["voxonta-matrix-app"]
