FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests==2.32.3

WORKDIR /app
COPY repair.py /app/repair.py
RUN chmod +x /app/repair.py

ENV REPAIR_FFPROBE=/usr/bin/ffprobe \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-u", "/app/repair.py"]
