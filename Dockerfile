FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/instance /app/cache

ENV ALLOW_MULTI_INSTANCE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 5005

CMD ["python", "empires-server.py", "--no-popup", "--host", "0.0.0.0", "--port", "5005", "--no-crash-log"]
