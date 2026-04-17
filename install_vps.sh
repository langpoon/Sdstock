#!/bin/bash
# KRSCAN VPS 설치 스크립트 (Ubuntu 20.04/22.04)
set -e

echo "=============================="
echo "  KRSCAN 설치 시작"
echo "=============================="

# 1. Python & pip
apt-get update -q
apt-get install -y python3 python3-pip python3-venv

# 2. 가상환경 생성
python3 -m venv /opt/krscan/venv
source /opt/krscan/venv/bin/activate

# 3. 패키지 설치
pip install --upgrade pip
pip install -r /opt/krscan/requirements.txt

# 4. 데이터 디렉토리
mkdir -p /opt/krscan/data

# 5. systemd 서비스 등록
cat > /etc/systemd/system/krscan.service << 'EOF'
[Unit]
Description=KRSCAN Stock Scanner
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/krscan
Environment="KRSCAN_DATA_DIR=/opt/krscan/data"
ExecStart=/opt/krscan/venv/bin/gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 2 \
    --timeout 120 \
    --access-logfile /opt/krscan/access.log \
    --error-logfile /opt/krscan/error.log \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable krscan
systemctl start krscan

echo ""
echo "=============================="
echo "  ✅ KRSCAN 설치 완료!"
echo "  브라우저: http://$(curl -s ifconfig.me):5000"
echo "=============================="
