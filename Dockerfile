# Multi-arch base image — runs natively on Oracle Ampere (ARM64) and x86.
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; httpx[http2] needs no compiler with wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
