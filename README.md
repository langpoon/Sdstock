# 🚀 KRSCAN 배포 가이드

한국 주식 + 코인 스캐너 웹앱 — 실서버 배포 패키지

---

## 📁 파일 구성

```
krscan_deploy/
├── app.py              ← 메인 앱 (전체 소스)
├── requirements.txt    ← Python 패키지 목록
├── Dockerfile          ← Docker 이미지 빌드
├── docker-compose.yml  ← Docker Compose 설정
├── nginx.conf          ← Nginx 리버스 프록시 설정
├── install_vps.sh      ← VPS 자동 설치 스크립트
└── README.md           ← 이 파일
```

---

## 🥇 방법 1: Docker (가장 쉬움)

### 준비
- VPS 서버 (Ubuntu 20.04+) 또는 로컬 PC
- Docker + Docker Compose 설치

```bash
# Docker 설치 (Ubuntu)
curl -fsSL https://get.docker.com | sh
apt install docker-compose-plugin -y
```

### 배포

```bash
# 1. 파일 업로드 (scp 또는 FTP)
scp -r krscan_deploy/ root@서버IP:/opt/krscan/

# 2. 서버 접속
ssh root@서버IP
cd /opt/krscan

# 3. 실행
docker compose up -d

# 4. 로그 확인
docker compose logs -f
```

### 접속
```
http://서버IP:5000
```

---

## 🥈 방법 2: VPS 직접 설치 (Python)

```bash
# 1. 파일 업로드
scp -r krscan_deploy/ root@서버IP:/opt/krscan/

# 2. 설치 스크립트 실행
ssh root@서버IP
cd /opt/krscan
chmod +x install_vps.sh
./install_vps.sh

# 3. 서비스 상태 확인
systemctl status krscan
```

---

## 🥉 방법 3: 무료 클라우드 플랫폼

### Railway.app (추천 - 무료)
1. https://railway.app 가입
2. "New Project" → "Deploy from GitHub"
3. 이 폴더를 GitHub에 업로드 후 연결
4. 환경변수 `KRSCAN_DATA_DIR=/app/data` 추가
5. 자동 배포 완료

### Render.com (무료)
1. https://render.com 가입
2. "New Web Service" 생성
3. Start Command: `gunicorn --bind 0.0.0.0:$PORT app:app`
4. 환경변수 `KRSCAN_DATA_DIR=/opt/render/project/data` 추가

### fly.io (무료 tier)
```bash
# fly CLI 설치 후
fly launch
fly deploy
```

---

## ⚙️ 도메인 연결 (선택)

```bash
# nginx 활성화
docker compose --profile nginx up -d

# nginx.conf 수정
server_name krscan.yourdomain.com;

# Let's Encrypt HTTPS (무료)
apt install certbot python3-certbot-nginx -y
certbot --nginx -d krscan.yourdomain.com
```

---

## 📊 데이터 파일 위치

배포 후 아래 파일들이 자동 생성됩니다:

| 파일 | 내용 |
|---|---|
| `data/users.json` | 계정 정보 |
| `data/email_config.json` | SMTP 설정 |
| `data/server_favorites.json` | 즐겨찾기 |
| `data/price_history.json` | 가격 이력 |
| `data/alert_conditions.json` | 조건 알림 |
| `data/.secret_key` | Flask 세션 키 |

> ⚠️ 백업 시 `data/` 폴더 전체를 백업하세요

---

## 🔐 보안 설정

배포 전 `app.py`에서 반드시 변경:

```python
ADMIN_ID = "langpoon"   # ← 원하는 관리자 ID로 변경
```

---

## 📞 포트 설정

| 환경 | 포트 |
|---|---|
| 로컬 개발 | `localhost:5000` |
| VPS (방화벽 오픈 필요) | `서버IP:5000` |
| Nginx 연결 시 | `80` / `443` |

VPS 방화벽 오픈:
```bash
ufw allow 5000/tcp
ufw allow 80/tcp
ufw allow 443/tcp
```

---

## 🛠️ 관리 명령어

```bash
# Docker 사용 시
docker compose restart    # 재시작
docker compose logs -f    # 로그 보기
docker compose down       # 중지
docker compose up -d      # 백그라운드 시작

# systemd 사용 시
systemctl restart krscan  # 재시작
journalctl -u krscan -f   # 로그 보기
systemctl stop krscan     # 중지
```
