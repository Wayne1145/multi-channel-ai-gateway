FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
COPY web ./web
RUN chown -R app:app /app
USER app
ENV PYTHONPATH=/app/src
CMD ["uvicorn", "wecom_ai_gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
