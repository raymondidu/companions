FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -m appuser
ARG GIT_SHA=UNKNOWN
ENV COMPANION_DEPLOY_SHA=${GIT_SHA}
COPY . .
RUN chown -R appuser:appuser /app
USER appuser
CMD ["python","companion_runner.py"]
