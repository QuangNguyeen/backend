# Kế hoạch Deploy Full-stack — DictaLearn (100 user, ngân sách ≤ 40$/tháng)

> Mở rộng từ [`DEPLOY_PLAN.md`](./DEPLOY_PLAN.md) (chỉ backend, ≤ 20$). Bản này bổ sung
> **frontend**, nâng cấu hình để phục vụ **~100 user hoạt động**, và nới ngân sách lên **40$/tháng**
> để có dư địa cho RAM, backup và độ tin cậy.

## 1. Bối cảnh & thành phần hệ thống

| Thành phần | Công nghệ | Ghi chú triển khai |
|---|---|---|
| **Frontend** | Vite + React 19 SPA (build tĩnh ra `dist/`) | Host tĩnh — Cloudflare Pages (free) **hoặc** Caddy phục vụ `dist/` trên VPS |
| **Backend web** | FastAPI / uvicorn (+ SSE `/api/v1/events/videos`) | `app/main.py`, health tại `/health` |
| **Message queue / worker** | Celery (`pool=solo`) — pipeline STT AssemblyAI + làm giàu từ vựng | `app/celery_app.py:25`, tasks `transcription` + `vocabulary_enrichment` |
| **Database** | PostgreSQL 16 | driver async `asyncpg` (web) + sync `psycopg2` (worker) |
| **Broker + Pub/Sub** | Redis 7 | Celery broker/result **và** pub/sub cho SSE (`app/events.py`) |
| **Phụ thuộc hệ thống** | `ffmpeg` (yt-dlp), spaCy `en_core_web_sm` | cần trong image worker |

Hai luồng đặc thù phải giữ đúng khi deploy:
1. **SSE real-time** (`/api/v1/events/videos`) — reverse proxy **không được buffer** (Caddy `flush_interval -1`).
2. **Worker STT** — tải audio YouTube qua `cookies.txt` (`app/services/youtube_service.py:22`); cookies hết hạn cần làm mới.

## 2. Ngân sách (mục tiêu ≤ 40$/tháng cho ~100 user)

### Phương án A — Một VPS + Frontend tĩnh trên CDN (KHUYẾN NGHỊ)

| Hạng mục | Lựa chọn | ~Chi phí/tháng |
|---|---|---|
| VPS | **Hetzner CPX31** (4 vCPU AMD / 8 GB / 160 GB NVMe) | ~16$ |
| Auto-backup VPS | Snapshot Hetzner (+20%) | ~3$ |
| Frontend hosting | **Cloudflare Pages** (build SPA, CDN toàn cầu) | **0$ (free tier)** |
| Domain | `.com` qua Cloudflare Registrar (tính trung bình/tháng) | ~1$ |
| Backup DB off-site | Cloudflare R2 / Backblaze B2 (`pg_dump` hằng đêm) | ~1$ |
| **Tổng hạ tầng** | | **~21$/tháng** |
| **Dư ngân sách** | nâng VPS lên CPX41 (8 vCPU/16 GB) hoặc tách managed DB | tối đa ~19$ |

8 GB RAM thoải mái cho 100 user: Postgres + Redis + uvicorn (nhiều worker) + 1 job STT đồng thời
(spaCy ~50 MB + audio tạm). 4 vCPU đủ cho uvicorn đa worker **và** transcription chạy song song.

### Phương án B — Tách Database managed (độ tin cậy cao hơn)

| Hạng mục | Lựa chọn | ~Chi phí/tháng |
|---|---|---|
| VPS app (web + worker + redis) | Hetzner CPX21 (3 vCPU / 4 GB) | ~9$ |
| Managed PostgreSQL | DigitalOcean Managed DB (1 GB) | ~15$ |
| Frontend | Cloudflare Pages | 0$ |
| Backup + domain | snapshot + R2 + domain | ~5$ |
| **Tổng** | | **~29$/tháng** |

> Chọn **A** cho MVP 100 user (rẻ, đơn giản, đủ mạnh). Chuyển sang **B** khi DB trở thành điểm
> nghẽn hoặc cần backup/HA do nhà cung cấp lo. Cả hai đều ≤ 40$.

### Lưu ý chi phí AI (ngoài ngân sách hạ tầng)
AssemblyAI / Gemini / Google STT tính theo mức sử dụng, **không** nằm trong 40$ này. Với 100 user
cần đặt hạn mức (quota) và theo dõi để tránh vượt chi phí biến đổi.

## 3. Kiến trúc triển khai (Phương án A)

```
                         ┌─────────────────────────────┐
   Người dùng ─────────▶ │ Cloudflare Pages (CDN, free)│  ← Frontend SPA (dist/)
                         └──────────────┬──────────────┘
                                        │ gọi API (https://api.<domain>)
                                        ▼
   Internet ──▶ Caddy (:80/:443, auto-HTTPS) ──▶ web (uvicorn :8000, N workers)
                   │ flush_interval -1 cho /events  │
   ┌───────────────┴───────────────┬────────────────┼─────────────────┐
   ▼                ▼               ▼                ▼                  ▼
 worker (celery)  postgres:16    redis:7      (mạng docker chung)   volume: pgdata
   ffmpeg+spaCy   pgdata vol     broker+SSE
   +yt-dlp(cookies)              maxmem 512mb
```

Một Docker image dùng chung cho `web` và `worker` (cùng code, khác lệnh khởi động).

## 4. Các file cần tạo / sửa

### Backend (image + compose)

1. **`Dockerfile`** (thay stub hiện tại `FROM ubuntu; ENTRYPOINT top`)
   - Base `python:3.12-slim`; `apt-get install ffmpeg build-essential libpq-dev curl` → dọn apt lists.
   - `pip install -r requirements.txt` → `python -m spacy download en_core_web_sm`.
   - Copy `app/`, `alembic/`, `alembic.ini`, `scripts/docker-entrypoint.sh`.
   - User non-root; `EXPOSE 8000`; `CMD` mặc định chạy entrypoint (web).

2. **`scripts/docker-entrypoint.sh`** (viết lại — file hiện tại bị đảo thứ tự dòng)
   - `set -e`; chờ Postgres sẵn sàng (poll asyncpg); `alembic upgrade head`; rồi
     `exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips '*'`.
   - **Chỉ web chạy migration** (worker không, tránh race).
   - `--workers 4` (tận dụng 4 vCPU) — chịu tải ~100 user; điều chỉnh theo RAM thực tế.

3. **`docker-compose.prod.yml`** (mới) — mọi service `restart: unless-stopped`:
   - **caddy** — `caddy:2-alpine`, ports `80:80`/`443:443`, mount `Caddyfile` + volume `caddy_data`/`caddy_config`.
   - **web** — image build, `env_file: .env`, `depends_on: [db, redis]` (healthcheck), healthcheck `GET /health`. Không expose cổng host.
   - **worker** — cùng image, lệnh `celery -A app.celery_app.celery worker --loglevel=info --pool=prefork --concurrency=3` (xử lý **3 transcription song song**), mount `cookies.txt` (ro) + JSON credential GCP (secret), `depends_on: [db, redis]`. Có thể nâng throughput thêm bằng `docker compose up -d --scale worker=2` (2 container × 3 = 6 job đồng thời).
     - ⚠️ **Bắt buộc sửa `app/celery_app.py:25`**: bỏ/đổi `worker_pool="solo"` → `worker_pool="prefork"`. Pool `solo` **bỏ qua** `--concurrency`, nên giữ `solo` thì cờ tăng worker vô tác dụng. Giữ nguyên `task_acks_late=True` + `worker_prefetch_multiplier=1` để mỗi process nhận đúng 1 task.
   - **db** — `postgres:16-alpine`, volume `pgdata`, healthcheck `pg_isready`, tinh chỉnh `shared_buffers`/`max_connections` cho 8 GB.
   - **redis** — `redis:7-alpine`, `--maxmemory 512mb --maxmemory-policy allkeys-lru`, volume `redis_data`, healthcheck `redis-cli ping`.

4. **`Caddyfile`** (mới)
   - `api.<domain> { reverse_proxy web:8000 }` — auto TLS Let's Encrypt.
   - Với path events: `reverse_proxy web:8000 { flush_interval -1 }` để SSE stream real-time.
   - (Tùy chọn) nếu host luôn frontend trên VPS: thêm block `<domain>` phục vụ `dist/` tĩnh.

5. **`.dockerignore`** (bổ sung) — loại `db_data/`, `cookies.txt`, `scripts/gen-lang-client-*.json`, `*.mp3`, `jenkins_home/`, `node_modules/`, file test/demo để secret/data không lọt vào image.

6. **Env / secret** — tạo `.env` trên server từ `.env.example`, bổ sung cho production:
   `GEMINI_API_KEY`, `GOOGLE_CLIENT_ID`, `ASSEMBLYAI_API_KEY`, `GCP_PROJECT_ID`, `GCS_BUCKET_NAME`,
   `GOOGLE_APPLICATION_CREDENTIALS` (đường dẫn JSON đã mount), `YOUTUBE_API_KEY`,
   `SECRET_KEY` thật (`openssl rand -hex 32`), `DEBUG=False`,
   `DATABASE_URL` trỏ service `db`, `REDIS_URL=redis://redis:6379/0`,
   `CORS_ORIGINS=["https://<domain>"]` (origin frontend production). Commit kèm `.env.production.example` (chỉ mô tả key).

### Frontend (Cloudflare Pages)

7. **Cấu hình build Pages** (kết nối repo frontend, hoặc deploy bằng `wrangler`)
   - Build command `npm ci && npm run build`, output `dist/` (Vite).
   - Biến môi trường build: `VITE_API_BASE_URL=https://api.<domain>/api/v1`, `VITE_GOOGLE_CLIENT_ID=...`.
   - SPA fallback: thêm `public/_redirects` với `/*  /index.html  200` cho react-router.
   - Sau khi có domain Pages, thêm origin đó vào `CORS_ORIGINS` của backend.

### CI/CD

8. **`.github/workflows/deploy.yml`** (backend, mới) — push lên `main`:
   - Chạy `pytest` (cổng kiểm tra) → build image → push lên **GHCR** (`ghcr.io/<owner>/dictalearn-backend:sha`).
   - SSH vào VPS (`appleboy/ssh-action`, secret `VPS_HOST`/`VPS_USER`/`VPS_SSH_KEY`):
     `docker compose -f docker-compose.prod.yml pull && up -d`. Entrypoint tự `alembic upgrade head`. `docker image prune -f`.
   - Frontend: Cloudflare Pages **tự build/deploy** khi push repo frontend (không cần workflow riêng).

9. **`scripts/provision.sh`** (chạy một lần) + **`DEPLOY.md`** ngắn
   - Tạo user sudo non-root, cài Docker + compose plugin, bật UFW (22/80/443), thêm **2 GB swap**,
     clone repo, tạo `.env`, đặt `cookies.txt` + JSON GCP, `docker compose -f docker-compose.prod.yml up -d`.

## 5. Sizing cho ~100 user

- **uvicorn `--workers 4`** trên 4 vCPU: phục vụ tốt tải đọc/ghi API thường (dictation, quiz, vocab).
  Mỗi worker ~150–250 MB → ~1 GB; còn dư cho Postgres/Redis.
- **SSE**: mỗi client giữ 1 kết nối mở. 100 kết nối SSE đồng thời nhẹ với uvicorn (async) — chỉ cần
  Redis pub/sub ổn định và Caddy không buffer.
- **Postgres**: `max_connections` ≥ 100 (4 web worker × pool + worker celery); cân nhắc thêm
  PgBouncer nếu pool cạn. Với MVP 100 user, mặc định + pool SQLAlchemy hợp lý là đủ.
- **Worker STT `--pool=prefork --concurrency=3`** = 3 transcription song song. STT chủ yếu I/O-bound
  (download audio + gọi API AssemblyAI), nên concurrency tăng throughput mà không nghẽn CPU; phần
  spaCy enrichment mới ăn CPU. Mỗi process fork ~50 MB spaCy → 3 × ~50 MB, vẫn nhẹ với 8 GB.
  - Khi 100 user import dồn dập: scale ngang `--scale worker=2` (6 job đồng thời) thay vì tăng
    `--concurrency` quá cao (tránh OOM do nhiều audio tạm cùng lúc). Theo dõi RSS + độ sâu hàng đợi Redis.
  - Nếu CPU bão hòa vì enrichment, nâng VPS lên **CPX41 (8 vCPU / 16 GB)** — vẫn ≤ 40$.
- **Redis `maxmemory 512mb`** + LRU: thoải mái cho broker + result + pub/sub ở quy mô này.

## 6. Rủi ro vận hành cần xử lý

1. **YouTube chặn bot (IP datacenter)** — mount `cookies.txt` mới vào worker; lên lịch làm mới định kỳ. Rủi ro độ tin cậy STT lớn nhất.
2. **Vệ sinh secret** — giữ `cookies.txt` & `gen-lang-client-*.json` ngoài git và ngoài image; chỉ mount runtime.
3. **CORS / domain** — `CORS_ORIGINS` phải khớp đúng domain Pages; `allow_credentials=True` không dùng được với `*`.
4. **Bộ nhớ khi transcribe** — `--concurrency=3` (+`--scale`) nghĩa là nhiều file audio tạm cùng lúc; theo dõi RSS, 2 GB swap là lưới an toàn; nâng CPX41 nếu OOM (vẫn ≤ 40$). Hạ concurrency nếu RSS sát ngưỡng.
5. **Tranh chấp migration** — chỉ entrypoint web chạy `alembic upgrade head`; worker chờ healthcheck DB.
6. **Backup DB** — bật auto-backup Hetzner **và** cron `pg_dump` hằng đêm đẩy ra R2/B2 off-site để khôi phục được.
7. **Chi phí AI biến đổi** — đặt quota AssemblyAI/Gemini/Google STT; theo dõi dashboard để không vượt chi phí.

## 7. Kiểm thử end-to-end

1. **Build local:** `docker build -t dictalearn .` → `docker compose -f docker-compose.prod.yml up` với `.env` test; web/worker/db/redis đều healthy.
2. **Health:** `curl https://api.<domain>/health` → `{"status":"ok",...}`; `GET /docs` tải được.
3. **Frontend:** mở `https://<domain>` (Pages) → SPA load, gọi được API, OAuth Google hoạt động.
4. **DB:** xác nhận `alembic upgrade head` đã chạy (bảng tồn tại; log container web).
5. **Worker:** trigger transcribe 1 video → `docker compose logs -f worker` thấy `Task started … Pipeline complete`; `transcription_status` pending→processing→ready.
6. **SSE:** `curl -N https://api.<domain>/api/v1/events/videos` giữ kết nối và phát event khi video cập nhật (kiểm chứng `flush_interval -1`).
7. **Tải nhẹ:** mô phỏng ~100 request đồng thời (k6/ab) tới endpoint dictation/quiz; xác nhận latency & không lỗi 5xx.
8. **CI/CD:** push thay đổi nhỏ lên `main` → Action build/push GHCR/SSH; container nhận image mới. Push frontend → Pages tự deploy.
9. **Phục hồi sau reboot:** `reboot` VPS → mọi container quay lại nhờ `restart: unless-stopped`.

## 8. Thứ tự thực hiện (checklist)

- [ ] Đăng ký VPS Hetzner CPX31 + domain (Cloudflare Registrar), bật auto-backup.
- [ ] Chạy `scripts/provision.sh` (Docker, UFW, swap, clone repo).
- [ ] Viết lại `Dockerfile`, `scripts/docker-entrypoint.sh`; tạo `docker-compose.prod.yml`, `Caddyfile`, cập nhật `.dockerignore`.
- [ ] Tạo `.env` production trên server + đặt `cookies.txt` & JSON GCP.
- [ ] `docker compose -f docker-compose.prod.yml up -d` → kiểm thử mục 7 (1–6).
- [ ] Tạo Cloudflare Pages cho frontend (build env `VITE_API_BASE_URL`), thêm domain Pages vào `CORS_ORIGINS`.
- [ ] Thêm `.github/workflows/deploy.yml` + secret repo → kiểm tra push-to-deploy.
- [ ] Bật cron `pg_dump` → R2/B2; đặt quota API AI.
- [ ] Chạy test tải mục 7 (7) và xác nhận reboot-resilience (9).
```