FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

EXPOSE 8787

CMD ["uvicorn", "app.main:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8787"]
