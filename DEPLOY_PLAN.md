# Kế hoạch Deploy — DictaLearn Backend trên một VPS duy nhất (≤ 20$/tháng)

## Bối cảnh

Backend là một ứng dụng đa tiến trình (multi-process): **web FastAPI/uvicorn** (đồng thời phục
vụ sự kiện real-time qua SSE), một **Celery worker** (pool `solo`) chạy pipeline STT của
AssemblyAI + làm giàu từ vựng (vocabulary enrichment), **PostgreSQL**, và **Redis** (dùng cho
hai mục đích — broker/result backend của Celery *và* pub/sub cho luồng SSE `/events/videos`).
Hệ thống còn cần **ffmpeg** ở mức hệ thống (yt-dlp trích xuất audio) và model spaCy
**`en_core_web_sm`**.

Một nền tảng PaaS quản lý sẽ tính phí ~7$/dịch vụ × 4 ≈ 28$+/tháng — vượt ngân sách. Phương án
rẻ và ổn định nhất là **chạy tất cả trên một VPS nhỏ bằng docker-compose**, dùng Caddy để tự
động cấp HTTPS và một pipeline deploy "push-to-deploy" bằng GitHub Actions. Cách này vẫn còn dư
nhiều ngân sách dưới mức 20$/tháng cho backup và domain. Các API AI bên ngoài (AssemblyAI /
Gemini / Google Cloud) tính phí theo mức sử dụng và được tính riêng, không nằm trong ngân sách
hạ tầng này.

Các tài nguyên deploy hiện có trong repo đang là stub/hỏng và cần làm lại:
- `Dockerfile` chỉ là placeholder (`FROM ubuntu; ENTRYPOINT top`).
- `scripts/docker-entrypoint.sh` bị đảo/lỗi thứ tự dòng; `scripts/start.sh` rỗng.
- ~~`docker-compose.local-ci.yml` là cấu hình Jenkins/nginx cho local~~ — **đã xóa**; thay bằng `docker-compose.prod.yml` + Caddy + GitHub Actions.
- `.dockerignore` **chưa** loại trừ `db_data/`, `cookies.txt`, hay file JSON credential của GCP.

## Ngân sách (mục tiêu ≤ 20$/tháng)

| Hạng mục | Lựa chọn | ~Chi phí/tháng |
|------|--------|----------|
| VPS | **Hetzner CX22** (2 vCPU / 4 GB / 40 GB, đã gồm IPv4) | ~5.50$ |
| Auto-backup | Snapshot Hetzner (+20%) | ~1.10$ |
| Domain | TLD giá rẻ, tính trung bình theo tháng (xem bên dưới) | ~1$ |
| **Tổng hạ tầng** | | **~8$/tháng** |
| Dư ngân sách | Nâng lên CPX21 (4 GB AMD) hoặc CX32 (8 GB) nếu STT/audio cần thêm RAM | tối đa ~11$ |

4 GB RAM là mức cân bằng lý tưởng: model spaCy (~50 MB resident) + file audio tạm + Postgres +
Redis chạy thoải mái. Máy 2 GB sẽ cần swap và có nguy cơ OOM khi transcribe, nên khuyến nghị
4 GB. **Nhà cung cấp thay thế:** droplet DigitalOcean 12$ (2 GB) — dùng được nhưng nên thêm swap.

### Gợi ý domain
- **Cloudflare Registrar** — bán đúng giá gốc, không cộng phí (~10$/năm cho `.com`), kèm DNS
  Cloudflare miễn phí.
- **Porkbun / Namecheap** — khuyến mãi năm đầu rẻ (`.xyz`/`.site` ~1–3$/năm) nếu muốn chi phí
  thấp nhất; phí gia hạn cao hơn.
- Khuyến nghị: đăng ký trên **Porkbun** hoặc **Cloudflare**, trỏ DNS (bản ghi A) về IP của VPS.
  Caddy sẽ tự xử lý TLS khi bản ghi A đã phân giải.

## Kiến trúc (một VPS, docker-compose)

```
Internet ──▶ Caddy (:80/:443, auto-HTTPS) ──▶ web (uvicorn :8000)
                                                  │
   ┌──────────────────────────────────────────────┼───────────────┐
   ▼                  ▼                  ▼          ▼
 worker (celery)   postgres:16       redis:7   (mạng docker chung)
   │  dùng redis broker + sync DB     volume: pgdata
   └─ ffmpeg + spaCy + yt-dlp (mount cookies)
```

Một Docker image dùng chung cho cả `web` và `worker` (cùng code, khác lệnh khởi động).

## Các file cần tạo / sửa

### 1. `Dockerfile` (thay thế stub)
- Base `python:3.12-slim`.
- `apt-get install ffmpeg` (+ `build-essential`, `libpq-dev`, `curl` cho healthcheck/build),
  sau đó dọn dẹp apt lists.
- `pip install -r requirements.txt` rồi `python -m spacy download en_core_web_sm`.
- Copy `app/`, `alembic/`, `alembic.ini`, `scripts/docker-entrypoint.sh`.
- Tạo user non-root; `EXPOSE 8000`; `CMD` mặc định chạy entrypoint (web). Worker ghi đè lệnh
  trong compose.

### 2. `scripts/docker-entrypoint.sh` (viết lại — file hiện tại bị lỗi thứ tự)
- `set -e`; chờ Postgres chấp nhận kết nối (lặp kết nối asyncpg, đúng như logic dự định trong
  file bị hỏng); chạy `alembic upgrade head`; sau đó
  `exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'`.
- Chỉ chạy migration ở đây (container web) để worker không bị tranh chấp (race).

### 3. `docker-compose.prod.yml` (mới)
Các service, tất cả đặt `restart: unless-stopped`:
- **caddy** — `caddy:2-alpine`, cổng `80:80`/`443:443`, mount `Caddyfile` + volume `caddy_data`/`caddy_config`.
- **web** — image đã build, `env_file: .env`, `depends_on: [db, redis]` (có healthcheck),
  healthcheck gọi `GET /health` (endpoint có sẵn tại `app/main.py:41`). Không mở cổng host (chỉ Caddy truy cập được).
- **worker** — cùng image, lệnh `celery -A app.celery_app.celery worker --loglevel=info --pool=solo`
  (khớp với `worker_pool="solo"` ở `app/celery_app.py:25`), `env_file: .env`,
  mount `cookies.txt` (chỉ đọc) và file JSON credential GCP dưới dạng secret, `depends_on: [db, redis]`.
- **db** — `postgres:16-alpine`, volume `pgdata`, healthcheck `pg_isready`, env user/pass/db.
- **redis** — `redis:7-alpine`, `--maxmemory 256mb --maxmemory-policy allkeys-lru`,
  volume `redis_data` tùy chọn (giữ bền broker), healthcheck `redis-cli ping`.

### 4. `Caddyfile` (mới)
- `your-domain.com { reverse_proxy web:8000 }` — tự động cấp TLS Let's Encrypt.
- **Quan trọng cho SSE:** với đường dẫn events, đặt `reverse_proxy` kèm `flush_interval -1`
  (tắt buffer response) để `/api/v1/events/videos` stream real-time.

### 5. Env / secret
- Tạo `.env` trên server (không commit) từ `.env.example`, bổ sung các giá trị production còn
  thiếu so với file mẫu: `GEMINI_API_KEY`, `GOOGLE_CLIENT_ID`, `ASSEMBLYAI_API_KEY`,
  `GCP_PROJECT_ID`, `GCS_BUCKET_NAME`, `GOOGLE_APPLICATION_CREDENTIALS` (đường dẫn tới JSON đã
  mount), `YOUTUBE_API_KEY`, một `SECRET_KEY` thật (`openssl rand -hex 32`), `DEBUG=False`,
  `DATABASE_URL` trỏ tới service `db`, `REDIS_URL=redis://redis:6379/0`, và `CORS_ORIGINS` liệt
  kê origin của frontend production.
- Cung cấp một file `.env.production.example` được commit, mô tả mọi key (không có giá trị).

### 6. `.dockerignore` (bổ sung)
Thêm các mục để secret/data không bao giờ lọt vào image hay context git:
`db_data/`, `cookies.txt`, `scripts/gen-lang-client-*.json`, `*.mp3`, `jenkins_home/`,
các file test/demo.

### 7. `.github/workflows/deploy.yml` (mới) — deploy khi push lên `main`
- Kích hoạt khi push lên `main`.
- Build image và push lên **GHCR** (`ghcr.io/<owner>/dictalearn-backend:sha`).
- SSH vào VPS (dùng `appleboy/ssh-action` + secret repo `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`):
  `docker compose -f docker-compose.prod.yml pull && up -d`. Entrypoint chạy
  `alembic upgrade head` khi web khởi động. Tùy chọn `docker image prune -f`.
- Thay thế hướng tiếp cận Jenkins trong repo bằng GitHub-hosted CI đơn giản, miễn phí.

### 8. `scripts/provision.sh` (mới, chạy một lần) + một file `DEPLOY.md` ngắn
Khởi tạo server lần đầu: tạo user sudo non-root, cài Docker + compose plugin, bật UFW (mở
22/80/443), thêm 2 GB swap (biên an toàn cho audio/STT khi tải cao), clone repo, tạo `.env`, đặt
`cookies.txt` + JSON GCP, `docker compose -f docker-compose.prod.yml up -d`.

## Rủi ro vận hành cần xử lý

1. **YouTube chặn bot** — IP datacenter thường gặp lỗi "Sign in to confirm you're not a bot".
   Code đã nạp `cookies.txt` (`app/services/youtube_service.py:22`); compose **phải mount một
   cookies.txt mới** vào worker, và cookies sẽ hết hạn — cần lên kế hoạch làm mới định kỳ. Đây
   là rủi ro độ tin cậy lớn nhất của tính năng STT.
2. **Vệ sinh secret** — `cookies.txt` và `scripts/gen-lang-client-*.json` hiện đang tồn tại
   trong thư mục repo nhưng chưa được track. Giữ chúng ngoài git và ngoài image
   (`.dockerignore`), chỉ mount lúc runtime.
3. **Mức song song của worker** — `--pool=solo` = mỗi lần một transcription. Phù hợp với máy
   nhỏ; video dài sẽ chặn hàng đợi. Chấp nhận được cho MVP; chỉ cân nhắc `--concurrency=2`
   (prefork) nếu RAM cho phép.
4. **Bộ nhớ** — theo dõi RSS khi transcribe; 2 GB swap là lưới an toàn. Nâng lên CPX21/CX32
   (vẫn ≤ 20$) nếu bị OOM.
5. **Tranh chấp migration** — chỉ entrypoint của web chạy `alembic upgrade head`; worker chờ
   healthcheck của DB và không chạy migration.
6. **Backup DB** — bật auto-backup của Hetzner *và* thêm cron `pg_dump` hằng đêm đẩy ra nơi lưu
   trữ rẻ ngoài máy (hoặc bucket GCS đã cấu hình) để có thể khôi phục.

## Kiểm thử (end-to-end)

1. **Kiểm tra build local:** `docker build -t dictalearn .` rồi
   `docker compose -f docker-compose.prod.yml up` ở local với `.env` test; xác nhận web,
   worker, db, redis đều đạt trạng thái healthy.
2. **Health:** `curl https://<domain>/health` → `{"status":"ok",...}`; `GET /docs` tải được.
3. **DB:** xác nhận `alembic upgrade head` đã chạy (bảng tồn tại; kiểm tra log container).
4. **Luồng worker:** kích hoạt transcribe một video qua API; xem `docker compose logs -f
   worker` thấy `[CELERY] ▶ Task started … ✔ Pipeline complete`; xác minh `transcription_status`
   chuyển pending→processing→ready trong DB.
5. **SSE:** `curl -N https://<domain>/api/v1/events/videos` giữ kết nối mở và phát sự kiện khi
   một video cập nhật (kiểm chứng `flush_interval -1` của Caddy).
6. **CI/CD:** push một thay đổi nhỏ lên `main`; xác nhận GitHub Action build, push lên GHCR,
   SSH vào server, và các container nhận image mới.
7. **Khả năng phục hồi sau reboot:** `reboot` VPS; xác nhận mọi container quay lại nhờ
   `restart: unless-stopped`.
8. **Test:** chạy `pytest` (`tests/`) hiện có trong CI như một cổng kiểm tra trước khi deploy.