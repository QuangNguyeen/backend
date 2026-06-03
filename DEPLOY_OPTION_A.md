# Runbook chi tiết — Deploy Option A (1 VPS Hetzner CPX31 + Cloudflare Workers)

> Triển khai cụ thể cho **Phương án A** trong [`DEPLOY_PLAN_FULLSTACK.md`](./DEPLOY_PLAN_FULLSTACK.md):
> ~100 user, ≤ 40$/tháng. Frontend tĩnh trên Cloudflare Workers (free); backend + worker + Postgres +
> Redis chạy docker-compose sau Caddy auto-HTTPS trên một VPS.
>
> Tài liệu này là **runbook chạy được**: copy nguyên các file, làm theo lệnh từ trên xuống.

---

## 0. Tổng quan

```
 user ─▶ Cloudflare Workers (https://app.<domain>) ─┐  frontend SPA (Vite build)
                                                    │  gọi API
                                                    ▼
 user ─▶ Caddy :443  ──▶ web (uvicorn :8000, 4 workers, /health, SSE)
            (api.<domain>)        │
   ┌─────────────────────┬────────┴───────┬─────────────────┐
   ▼                     ▼                ▼                 ▼
 worker (celery          postgres:16    redis:7        docker network
  prefork x3)            vol pgdata      broker+pubsub
  ffmpeg+spaCy+yt-dlp                    vol redis_data
```

**Domain dùng trong runbook** (đổi theo của bạn):
- Frontend: `app.dictalearn.com` (Cloudflare Workers Static Assets — hiện đang ở `dictalearn-react.nvquang176.workers.dev`)
- Backend API: `api.dictalearn.com` (VPS qua Caddy)

---

## 1. Chuẩn bị (một lần)

| Việc | Chi tiết |
|---|---|
| VPS | Tạo **Hetzner Cloud CPX31** (4 vCPU / 8 GB / 160 GB), Ubuntu 24.04, bật **Backups** khi tạo |
| Domain | Đăng ký + đưa DNS về **Cloudflare** |
| DNS records | `A  api.dictalearn.com → <IP VPS>` (proxy **OFF / DNS only** để Caddy tự xin TLS). `app` gắn qua Custom Domain của Workers ở §5.3 (Cloudflare tự tạo DNS) |
| SSH key | `ssh-keygen -t ed25519 -f ~/.ssh/dictalearn` → thêm public key vào VPS |
| Secret cần sẵn | `cookies.txt` (YouTube), `gen-lang-client-*.json` (GCP), các API key (Gemini, AssemblyAI, Google OAuth client id, YouTube) |

---

## 2. File backend cần tạo / sửa

Tạo các file sau trong repo `backend/` rồi commit (trừ `.env` thật & secret).

### 2.1 `Dockerfile` (thay thế stub hiện tại)

```dockerfile
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps: ffmpeg (yt-dlp), build/pg headers, curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m spacy download en_core_web_sm

# App code
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Non-root
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
```

### 2.2 `scripts/docker-entrypoint.sh` (viết lại — file hiện tại bị đảo dòng)

```bash
#!/usr/bin/env bash
set -e

echo "Waiting for database..."
python - <<'PY'
import asyncio, os, sys, time
import asyncpg
url = os.environ["DATABASE_URL"].replace("+asyncpg", "")
async def check():
    try:
        conn = await asyncpg.connect(url)
        await conn.close()
        return True
    except Exception:
        return False
for _ in range(60):
    if asyncio.run(check()):
        print("Database is ready!"); sys.exit(0)
    print("DB not ready, waiting..."); time.sleep(2)
print("DB never became ready"); sys.exit(1)
PY

# Chỉ web container chạy migration (worker bỏ qua bằng cách override CMD)
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "Running alembic upgrade head..."
  alembic upgrade head
fi

echo "Starting uvicorn..."
exec uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 \
  --workers "${UVICORN_WORKERS:-4}" \
  --proxy-headers --forwarded-allow-ips '*'
```

### 2.3 `app/celery_app.py` — đổi pool để bật concurrency (1 dòng)

```diff
-    worker_pool="solo",
+    worker_pool="prefork",
```

> Bắt buộc: pool `solo` **bỏ qua** `--concurrency`. Đổi sang `prefork` để worker STT chạy song song.
> Giữ nguyên `task_acks_late=True` + `worker_prefetch_multiplier=1`.

### 2.4 `docker-compose.prod.yml` (mới)

```yaml
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on: [web]

  web:
    image: ghcr.io/${GHCR_OWNER}/dictalearn-backend:${IMAGE_TAG:-latest}
    restart: unless-stopped
    env_file: .env
    environment:
      RUN_MIGRATIONS: "1"
      UVICORN_WORKERS: "4"
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 5
    expose: ["8000"]

  worker:
    image: ghcr.io/${GHCR_OWNER}/dictalearn-backend:${IMAGE_TAG:-latest}
    restart: unless-stopped
    env_file: .env
    environment:
      RUN_MIGRATIONS: "0"
    command: >
      celery -A app.celery_app.celery worker
      --loglevel=info --pool=prefork --concurrency=3
    volumes:
      - ./secrets/cookies.txt:/app/cookies.txt:ro
      - ./secrets/gcp-credentials.json:/app/gcp-credentials.json:ro
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }

  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    command: >
      postgres -c max_connections=120 -c shared_buffers=512MB
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru --appendonly yes
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  caddy_data:
  caddy_config:
  pgdata:
  redis_data:
```

> `GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-credentials.json` đặt trong `.env` (khớp mount ở worker).
> Container web/worker không expose cổng host — chỉ Caddy ra ngoài.

### 2.5 `Caddyfile` (mới) — chú ý SSE không buffer

```caddy
api.dictalearn.com {
    encode gzip

    # SSE: tắt buffer để stream real-time
    @sse path /api/v1/events/*
    reverse_proxy @sse web:8000 {
        flush_interval -1
    }

    reverse_proxy web:8000
}
```

### 2.6 `.dockerignore` (bổ sung các dòng)

```
db_data/
secrets/
cookies.txt
scripts/gen-lang-client-*.json
*.mp3
jenkins_home/
node_modules/
tests/
demo.py
scripts/demo_*.py
.env
.env.*
```

### 2.7 `.env.production.example` (commit — chỉ mô tả key, không giá trị)

```dotenv
# App
DEBUG=False
APP_NAME=DictaLearn API

# Database (trỏ tới service 'db')
DATABASE_URL=postgresql+asyncpg://dictalearn:CHANGE_ME@db:5432/dictalearn
POSTGRES_USER=dictalearn
POSTGRES_PASSWORD=CHANGE_ME
POSTGRES_DB=dictalearn

# JWT
SECRET_KEY=GENERATE_WITH_openssl_rand_hex_32
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# CORS — origin frontend production
CORS_ORIGINS=["https://app.dictalearn.com"]

# Redis (broker + SSE pub/sub)
REDIS_URL=redis://redis:6379/0

# AI / Google
GEMINI_API_KEY=
ASSEMBLYAI_API_KEY=
YOUTUBE_API_KEY=
GOOGLE_CLIENT_ID=
GCP_PROJECT_ID=
GCS_BUCKET_NAME=
GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-credentials.json

# CI/CD image
GHCR_OWNER=your-github-user
IMAGE_TAG=latest
```

---

## 3. Provision VPS (chạy một lần)

### 3.1 `scripts/provision.sh`

```bash
#!/usr/bin/env bash
set -e
# Chạy với quyền root trên VPS mới (Ubuntu 24.04)

# 1. User sudo non-root
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh

# 2. Docker + compose plugin
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy

# 3. Firewall
ufw allow 22 && ufw allow 80 && ufw allow 443
ufw --force enable

# 4. Swap 2 GB (an toàn khi transcribe)
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

echo "Provision done. Đăng nhập lại bằng user 'deploy'."
```

Chạy:
```bash
ssh root@<IP_VPS> 'bash -s' < scripts/provision.sh
```

### 3.2 Đưa code + secret lên server

```bash
ssh deploy@<IP_VPS>
git clone https://github.com/<owner>/dictalearn-backend.git ~/app
cd ~/app
mkdir -p secrets
# Từ máy local, copy secret lên (KHÔNG commit):
#   scp cookies.txt              deploy@<IP>:~/app/secrets/cookies.txt
#   scp gen-lang-client-xxx.json deploy@<IP>:~/app/secrets/gcp-credentials.json
cp .env.production.example .env
nano .env     # điền giá trị thật + SECRET_KEY (openssl rand -hex 32)
```

---

## 4. Deploy lần đầu

```bash
cd ~/app
# Login GHCR để pull image CI đã build (hoặc build tại chỗ - xem ghi chú)
echo $GHCR_PAT | docker login ghcr.io -u <owner> --password-stdin

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps      # tất cả 'healthy'
docker compose -f docker-compose.prod.yml logs -f web
```

> **Build tại chỗ thay vì pull** (nếu chưa có CI): bỏ dòng `image:` không liên quan và chạy
> `docker compose -f docker-compose.prod.yml up -d --build` sau khi thêm `build: .` vào service web/worker.

Migration `alembic upgrade head` chạy tự động trong entrypoint web.

---

## 5. Frontend — Cloudflare Workers (Static Assets, qua Wrangler)

> ✅ **Đã deploy thành công** (2026-06-03): worker `dictalearn-react`, live tại
> `https://dictalearn-react.nvquang176.workers.dev`. Dự án dùng **Cloudflare Workers Static Assets**
> (`npx wrangler deploy`), **không phải** Pages — nên **không cần** file `_redirects`. SPA fallback
> do `wrangler.jsonc` xử lý bằng `not_found_handling: "single-page-application"`.

### 5.1 `wrangler.jsonc` (Wrangler tự sinh — đã có)
```jsonc
{
  "name": "dictalearn-react",
  "compatibility_date": "2026-06-03",
  "observability": { "enabled": true },
  "assets": { "not_found_handling": "single-page-application" },
  "compatibility_flags": ["nodejs_compat"]
}
```

### 5.2 Build & deploy
- Build: `npm run build` (`tsc -b && vite build` → `dist/`).
- Deploy: `npx wrangler deploy` (CI Cloudflare đã chạy lệnh này tự động khi push).

### 5.3 Còn phải làm để vào production thật
1. **Biến môi trường build** (Workers → Settings → Variables/Build, hoặc `vars` trong wrangler):
   - `VITE_API_BASE_URL = https://api.dictalearn.com/api/v1`
   - `VITE_GOOGLE_CLIENT_ID = <client id>`
   > Vite nhúng `VITE_*` lúc **build**, không phải runtime — phải đặt trước khi build/deploy lại.
2. **Custom domain**: Workers → `dictalearn-react` → Settings → **Domains & Routes** → Add
   `app.dictalearn.com` (Cloudflare tự thêm DNS + TLS). Thay cho URL `*.workers.dev`.
3. **Backend CORS**: đặt `CORS_ORIGINS=["https://app.dictalearn.com"]` trong `.env` (thêm cả
   `https://dictalearn-react.nvquang176.workers.dev` nếu vẫn test qua URL workers.dev) rồi
   `docker compose -f docker-compose.prod.yml up -d web`.
4. **Google OAuth**: thêm `https://app.dictalearn.com` vào *Authorized JavaScript origins* trong
   Google Cloud Console (nếu không OAuth sẽ bị chặn origin).

### 5.4 Ghi chú tối ưu (không chặn deploy)
Build cảnh báo vài chunk > 500 kB — nặng nhất là `dash.all.min` (993 kB) và `hls` (523 kB) từ
`react-player`. Hiện đã code-split theo route (nhiều file `*Page-*.js`), nên các chunk media chỉ
tải khi vào trang video. Nếu muốn nhẹ hơn nữa: lazy-import `react-player` hoặc cấu hình
`manualChunks`. Không cần thiết cho 100 user — Cloudflare CDN + gzip đã phục vụ tốt.

---

## 6. CI/CD — `.github/workflows/deploy.yml`

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt && python -m spacy download en_core_web_sm
      - run: pytest -q

  build-and-deploy:
    needs: test
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ github.repository_owner }}/dictalearn-backend:latest
            ghcr.io/${{ github.repository_owner }}/dictalearn-backend:${{ github.sha }}
      - uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd ~/app
            docker compose -f docker-compose.prod.yml pull
            docker compose -f docker-compose.prod.yml up -d
            docker image prune -f
```

Secrets repo cần đặt: `VPS_HOST`, `VPS_USER` (=`deploy`), `VPS_SSH_KEY` (private key).
Frontend **không cần** workflow — Cloudflare Workers tự build + `wrangler deploy` khi push repo frontend (đã hoạt động).

---

## 7. Backup database (cron hằng đêm → off-site)

`scripts/backup-db.sh` trên VPS:
```bash
#!/usr/bin/env bash
set -e
cd ~/app
TS=$(date +%F)
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U dictalearn dictalearn | gzip > /tmp/db-$TS.sql.gz
# Đẩy lên R2/B2 (rclone đã cấu hình remote 'backup')
rclone copy /tmp/db-$TS.sql.gz backup:dictalearn-backups/
rm -f /tmp/db-$TS.sql.gz
# Giữ 14 ngày trên remote
rclone delete --min-age 14d backup:dictalearn-backups/
```
Cron: `crontab -e` → `0 3 * * * /home/deploy/app/scripts/backup-db.sh >> ~/backup.log 2>&1`

---

## 7.5 Tool xem log server (Dozzle) + uptime (Uptime Kuma)

Cần một cách xem log container qua trình duyệt thay vì SSH mỗi lần. **Dozzle** là lựa chọn nhẹ
nhất (~10 MB RAM, không cần agent/DB) — đọc trực tiếp log Docker, xem real-time, filter, tìm kiếm.
Kèm **Uptime Kuma** để theo dõi `/health` và báo động (Telegram/email) khi service chết.

### 7.5.1 Thêm service vào `docker-compose.prod.yml`

```yaml
  dozzle:
    image: amir20/dozzle:latest
    restart: unless-stopped
    environment:
      DOZZLE_NO_ANALYTICS: "true"
      # Chỉ hiện log các container của project này
      DOZZLE_FILTER: "label=com.docker.compose.project"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro   # chỉ đọc
    expose: ["8080"]

  uptime-kuma:
    image: louislam/uptime-kuma:1
    restart: unless-stopped
    volumes:
      - uptime_data:/app/data
    expose: ["3001"]
```

Thêm vào khối `volumes:` ở cuối file:
```yaml
  uptime_data:
```

> Mount `docker.sock` ở chế độ `:ro`. Dozzle không sửa container, chỉ đọc log. Không expose cổng
> host — chỉ truy cập qua Caddy đã bảo vệ bằng mật khẩu.

### 7.5.2 Bảo vệ bằng Caddy basic-auth (subdomain riêng)

Tạo hash mật khẩu:
```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'MAT_KHAU_MANH'
```

Thêm vào `Caddyfile` (nhớ tạo DNS `A logs.dictalearn.com` và `A status.dictalearn.com` → IP VPS):
```caddy
logs.dictalearn.com {
    basic_auth {
        admin <PASTE_HASH_Ở_TRÊN>
    }
    reverse_proxy dozzle:8080
}

status.dictalearn.com {
    basic_auth {
        admin <PASTE_HASH>
    }
    reverse_proxy uptime-kuma:3001
}
```

Áp dụng: `docker compose -f docker-compose.prod.yml up -d`.

### 7.5.3 Dùng

- **Log**: mở `https://logs.dictalearn.com` → đăng nhập → chọn container `web`/`worker`/`db`/`redis`,
  xem real-time, search, tải xuống. Tiện nhất để theo dõi pipeline STT của `worker`.
- **Uptime**: `https://status.dictalearn.com` → tạo monitor HTTP(s) tới
  `https://api.dictalearn.com/health` (interval 60s) → gắn thông báo Telegram/Discord/email.

### 7.5.4 Lựa chọn thay thế (không cài thêm service)

| Nhu cầu | Lệnh / tool |
|---|---|
| Xem nhanh qua SSH | `docker compose -f docker-compose.prod.yml logs -f web worker` |
| Top container kiểu htop | `ctop` (`docker run --rm -ti -v /var/run/docker.sock:/var/run/docker.sock quay.io/vektorlab/ctop`) |
| Giới hạn dung lượng log (tránh đầy đĩa) | thêm vào **mỗi service** trong compose:<br>`logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }` |
| Lưu trữ + truy vấn log dài hạn | Grafana **Loki + Promtail + Grafana** (nặng hơn ~300 MB RAM; chỉ cần khi muốn giữ log nhiều ngày / dashboard) |

> Khuyến nghị MVP 100 user: **Dozzle + Uptime Kuma + `max-size` log rotation** là đủ và gần như
> miễn phí RAM. Để dành Loki khi thật sự cần lưu trữ/điều tra log lịch sử.

---

## 8. Kiểm thử & nghiệm thu

| # | Kiểm tra | Lệnh / kỳ vọng |
|---|---|---|
| 1 | Health | `curl https://api.dictalearn.com/health` → `{"status":"ok"}` |
| 2 | Docs | `GET https://api.dictalearn.com/docs` tải được |
| 3 | Migration | `docker compose ... logs web` thấy `alembic upgrade head` xong, bảng tồn tại |
| 4 | Frontend | Mở `https://app.dictalearn.com` → SPA load, gọi API OK, Google OAuth OK |
| 5 | Worker STT | Trigger transcribe → `logs -f worker` thấy `Task started … Pipeline complete`; status pending→processing→ready |
| 6 | Concurrency | Trigger 3 video → 3 task chạy song song trong log worker |
| 7 | SSE | `curl -N https://api.dictalearn.com/api/v1/events/videos` giữ kết nối, có event khi video đổi |
| 8 | Tải | `k6`/`ab` ~100 request đồng thời tới endpoint dictation → không 5xx, latency ổn |
| 9 | CI/CD | Push nhỏ lên `main` → Action build/push/SSH; container nhận image mới |
| 10 | Reboot | `sudo reboot` → mọi container quay lại (`restart: unless-stopped`) |
| 11 | Backup | Chạy `backup-db.sh` thủ công → file `.sql.gz` xuất hiện trên R2/B2 |
| 12 | Log UI | `https://logs.dictalearn.com` (basic-auth) → thấy log real-time `web`/`worker` |
| 13 | Uptime | `https://status.dictalearn.com` → monitor `/health` xanh; tắt thử `web` → có cảnh báo |

---

## 9. Vận hành thường ngày

```bash
# Logs
docker compose -f docker-compose.prod.yml logs -f web worker

# Scale worker STT khi hàng đợi dài (2 container x concurrency 3 = 6 job)
docker compose -f docker-compose.prod.yml up -d --scale worker=2

# Update thủ công (ngoài CI)
docker compose -f docker-compose.prod.yml pull && \
docker compose -f docker-compose.prod.yml up -d

# Theo dõi tài nguyên (canh OOM khi transcribe)
docker stats

# Khôi phục DB từ backup
gunzip -c db-YYYY-MM-DD.sql.gz | \
docker compose -f docker-compose.prod.yml exec -T db psql -U dictalearn dictalearn
```

### Cảnh báo cần canh
- **RAM khi transcribe**: `--concurrency=3` = nhiều audio tạm cùng lúc. Nếu `docker stats` thấy
  worker sát ngưỡng → giảm concurrency hoặc nâng **CPX41 (16 GB)** (vẫn ≤ 40$).
- **DB connections**: 4 uvicorn worker × pool SQLAlchemy (mặc định 5+10) có thể tới ~60; đã set
  `max_connections=120`. Nếu cạn pool → cân nhắc PgBouncer hoặc giảm `UVICORN_WORKERS`.
- **cookies.txt hết hạn** → STT báo lỗi "Sign in to confirm…"; thay file mới trong `secrets/` rồi
  `up -d worker`.
- **Chi phí AI** → đặt quota AssemblyAI/Gemini/Google STT, theo dõi dashboard.

---

## 10. Tóm tắt thứ tự thực hiện

1. §1 Tạo VPS CPX31 + domain + DNS `api` + SSH key.
2. §2 Commit các file backend (Dockerfile, entrypoint, compose, Caddyfile, dockerignore, env example, sửa `celery_app.py`).
3. §3 `provision.sh` → clone repo → đặt secret → `.env`.
4. §6 Bật CI để build image lên GHCR (hoặc build tại chỗ).
5. §4 `docker compose up -d` → kiểm thử §8 (1–7).
6. §5 Cloudflare Workers cho frontend (đã deploy) → gắn custom domain + đặt `VITE_*` + thêm origin vào CORS.
7. §7 Cron backup + §8 (8–11) test tải/CI/reboot/backup.
8. §9 Bàn giao vận hành.
```