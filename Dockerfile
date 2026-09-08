FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd -m appuser \
    && mkdir -p /app/companion-data \
    && chown appuser:appuser /app/companion-data
ARG GIT_SHA=UNKNOWN
ENV COMPANION_DEPLOY_SHA=${GIT_SHA}
COPY . .
RUN chown -R appuser:appuser /app
RUN chmod 755 /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python","companion_runner.py"]
