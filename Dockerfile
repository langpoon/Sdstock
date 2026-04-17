FROM python:3.11-slim

WORKDIR /app

# 시스템 패키지
RUN apt-get update && apt-get install -y \
    gcc g++ curl && \
    rm -rf /var/lib/apt/lists/*

# Python 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 복사
COPY app.py .

# 데이터 디렉토리 (볼륨 마운트 대상)
RUN mkdir -p /app/data

# 포트
EXPOSE 5000

# 실행 — gunicorn (운영) 또는 flask (개발)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
