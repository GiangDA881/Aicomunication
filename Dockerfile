FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 agenthub
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
USER agenthub
EXPOSE 8000
CMD ["uvicorn", "agenthub.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
