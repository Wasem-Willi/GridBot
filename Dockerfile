FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY docs ./docs

ENV PYTHONPATH=/app/src
CMD ["python", "-m", "gridbot.main"]
